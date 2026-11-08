"""排班引擎（純 Python，不依賴任何外部求解套件，也不碰資料庫與介面）。

排班目標（由高到低）：
1. 硬性：每個班次、每個崗位、每個名額都有人；人員須具備該崗位資格；同一人同一班次只在一個崗位。
2. 班次數公平：每人輪到的班次盡量接近。
3. 休息分佈：盡量 1 休 1，避免連續多班；休息時間（共同休息、跨天）視為已休息。
4. 稀缺崗位專任：合格人數稀少的崗位，由這些人專任，不進入其他崗位的輪替，
   公平性分「專任組」與「一般組」各自計算。

做法：先把問題拆成互不重疊的「組」，每組以模擬退火（simulated annealing）決定「每個班次由哪些人上班」
（成本只與「誰在哪個班次上班」有關），再用匈牙利演算法把人員分配到各崗位座位（盡量讓同一人輪不同崗位）。
"""
from __future__ import annotations

import math
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

# 成本權重（數值越大越優先）
W_COUNT = 400.0  # 總班次數平方和：公平性
W_DAY = 15.0     # 單日班次數平方和：每天也盡量公平
W_RUN = 60.0     # 連續上班：每段連續 L 班扣 (L-1)^2
W_GAP = 8.0      # 休息間隔過長：每個 >=2 的間隔扣 (gap-1)^2（讓 1 休 1 比大間隔更好）

SeatKey = Tuple[int, int, int]  # (班次索引, 崗位 id, 第幾位)


@dataclass
class SlotInfo:
    day: int
    tpos: int
    label: str = ""


@dataclass
class PositionInfo:
    id: int
    name: str
    headcount: int = 1
    dedicated: Optional[bool] = None  # None 自動 / True 強制專任 / False 強制不專任


@dataclass
class StaffInfo:
    id: int
    name: str
    qualified: FrozenSet[int]


@dataclass
class Problem:
    slots: List[SlotInfo]
    day_lengths: List[int]
    positions: List[PositionInfo]
    staff: List[StaffInfo]
    locked: Dict[SeatKey, int] = field(default_factory=dict)  # 鎖定的座位 -> 人員 id
    avoid_consecutive: bool = True
    day_labels: List[str] = field(default_factory=list)


@dataclass
class SolveOptions:
    time_limit: float = 6.0
    max_iters: int = 250000
    restarts: int = 3
    seed: Optional[int] = None


@dataclass
class SolveResult:
    ok: bool
    assignments: Dict[SeatKey, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


@dataclass
class Analysis:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    counts: Dict[int, int] = field(default_factory=dict)
    day_counts: Dict[int, Dict[int, int]] = field(default_factory=dict)
    max_run: Dict[int, int] = field(default_factory=dict)
    bad_keys: Set[SeatKey] = field(default_factory=set)


# ---------------------------------------------------------------------------
# 共用小工具
# ---------------------------------------------------------------------------

def _seat_text(pos: PositionInfo, k: int) -> str:
    return f"第{k + 1}位" if pos.headcount > 1 else ""


def _day_label(problem: Problem, d: int) -> str:
    if 0 <= d < len(problem.day_labels):
        return problem.day_labels[d]
    return f"第{d + 1}天"


def _perfect_match(seat_pos: Sequence[int], persons: Sequence[int], qual: Dict[int, Set[int]]):
    """座位對人員的完美配對（Kuhn）。

    回傳 (配對清單 seat->person, None)；失敗時回傳 (None, (座位集合, 可用人員集合))。
    """
    n = len(seat_pos)
    if n > len(persons):
        # 人數不夠一定失敗；仍跑一次以取得 Hall 違反集合
        pass
    adj = [[p for p in persons if p in qual[pos]] for pos in seat_pos]
    match_p: Dict[int, int] = {}

    def try_seat(u: int, seen: Set[int]) -> bool:
        for p in adj[u]:
            if p in seen:
                continue
            seen.add(p)
            if p not in match_p or try_seat(match_p[p], seen):
                match_p[p] = u
                return True
        return False

    for u in range(n):
        seen: Set[int] = set()
        if not try_seat(u, seen):
            seats = {u} | {match_p[p] for p in seen if p in match_p}
            return None, (seats, seen)
    result = [-1] * n
    for p, u in match_p.items():
        result[u] = p
    return result, None


def _hungarian(cost: List[List[float]]) -> List[int]:
    """方陣最小成本指派；回傳 row -> col。"""
    n = len(cost)
    INF = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)
    way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    ans = [0] * n
    for j in range(1, n + 1):
        ans[p[j] - 1] = j - 1
    return ans


# ---------------------------------------------------------------------------
# 驗證與分析
# ---------------------------------------------------------------------------

def analyze(problem: Problem, assignments: Dict[SeatKey, int]) -> Analysis:
    """檢查一份班表（可為手動調整後的）：硬性違規列為 errors，軟性問題列為 warnings。"""
    staff_by_id = {s.id: s for s in problem.staff}
    res = Analysis()
    works: Dict[int, List[int]] = defaultdict(list)  # 人員 -> 班次索引

    for si, slot in enumerate(problem.slots):
        seen: Dict[int, SeatKey] = {}
        for pos in problem.positions:
            for k in range(pos.headcount):
                key = (si, pos.id, k)
                sid = assignments.get(key)
                where = f"{_day_label(problem, slot.day)} {slot.label}「{pos.name}」{_seat_text(pos, k)}"
                if sid is None:
                    res.errors.append(f"{where} 尚未安排人員")
                    res.bad_keys.add(key)
                    continue
                st = staff_by_id.get(sid)
                if st is None:
                    res.errors.append(f"{where} 的人員不是本次出席人員")
                    res.bad_keys.add(key)
                    continue
                if pos.id not in st.qualified:
                    res.errors.append(f"{where}：{st.name} 不具備此崗位資格")
                    res.bad_keys.add(key)
                if sid in seen:
                    res.errors.append(
                        f"{_day_label(problem, slot.day)} {slot.label}：{st.name} 同時被安排在兩個崗位"
                    )
                    res.bad_keys.add(key)
                    res.bad_keys.add(seen[sid])
                else:
                    seen[sid] = key
                    works[sid].append(si)

    for st in problem.staff:
        slots_worked = sorted(set(works.get(st.id, [])))
        res.counts[st.id] = len(slots_worked)
        per_day: Dict[int, int] = defaultdict(int)
        by_day_tpos: Dict[int, List[int]] = defaultdict(list)
        for si in slots_worked:
            sl = problem.slots[si]
            per_day[sl.day] += 1
            by_day_tpos[sl.day].append(sl.tpos)
        res.day_counts[st.id] = dict(per_day)
        longest = 0
        for d, tps in by_day_tpos.items():
            tps.sort()
            run = 1
            best = 1
            for x, y in zip(tps, tps[1:]):
                run = run + 1 if y == x + 1 else 1
                best = max(best, run)
            longest = max(longest, best)
            if problem.avoid_consecutive and best >= 3:
                res.warnings.append(f"{st.name} 在 {_day_label(problem, d)} 連續上 {best} 班")
        res.max_run[st.id] = longest
    return res


# ---------------------------------------------------------------------------
# 問題驗證
# ---------------------------------------------------------------------------

def _validate_problem(problem: Problem) -> List[str]:
    errs: List[str] = []
    if not problem.slots:
        errs.append("沒有任何班次可排，請確認工作日期與時間設定")
    if not problem.positions:
        errs.append("尚未選擇本次所需崗位")
    if not problem.staff:
        errs.append("尚未選擇本次出席人員")
    if errs:
        return errs
    for pos in problem.positions:
        if pos.headcount < 1:
            errs.append(f"崗位「{pos.name}」每班人數必須至少 1 人")

    staff_by_id = {s.id: s for s in problem.staff}
    pos_by_id = {p.id: p for p in problem.positions}
    for pos in problem.positions:
        q = sum(1 for s in problem.staff if pos.id in s.qualified)
        if q < pos.headcount:
            errs.append(
                f"崗位「{pos.name}」每班需要 {pos.headcount} 人，但本次出席人員中具備資格的只有 {q} 人"
            )

    seat_of_person: Dict[Tuple[int, int], SeatKey] = {}
    for key, sid in problem.locked.items():
        si, pid, k = key
        if not (0 <= si < len(problem.slots)) or pid not in pos_by_id or not (0 <= k < pos_by_id[pid].headcount):
            errs.append("存在無效的鎖定座位，請清除班表後重試")
            continue
        st = staff_by_id.get(sid)
        if st is None:
            errs.append(f"鎖定的人員不是本次出席人員（{problem.slots[si].label}）")
            continue
        if pid not in st.qualified:
            errs.append(f"鎖定的安排有誤：{st.name} 不具備「{pos_by_id[pid].name}」資格")
        if (si, sid) in seat_of_person:
            errs.append(f"{problem.slots[si].label}：{st.name} 被鎖定在兩個崗位")
        seat_of_person[(si, sid)] = key
    return errs


# ---------------------------------------------------------------------------
# 分組（專任 / 一般）
# ---------------------------------------------------------------------------

@dataclass
class _Group:
    name: str
    pos_ids: List[int]
    staff_idx: List[int]


def _choose_dedicated(problem: Problem, qual_count: Dict[int, int], notes: List[str]) -> Set[int]:
    forced = {p.id for p in problem.positions if p.dedicated is True}
    auto = {
        p.id
        for p in problem.positions
        if p.dedicated is None and qual_count[p.id] <= p.headcount + 1
    }
    chosen = forced | auto
    if not auto or len(chosen) == len(problem.positions):
        return chosen

    # 自動判定的專任，不可讓一般崗位的人力過緊（至少要比每班所需人數多 1 人才有得休息）
    ded_staff = {
        i for i, s in enumerate(problem.staff) if any(pid in s.qualified for pid in chosen)
    }
    rest_pos = [p for p in problem.positions if p.id not in chosen]
    seats = sum(p.headcount for p in rest_pos)
    rest_staff = [
        i
        for i, s in enumerate(problem.staff)
        if i not in ded_staff and any(p.id in s.qualified for p in rest_pos)
    ]
    if len(rest_staff) < seats + 1:
        notes.append("依合格人數判定的專任崗位會讓其他崗位人力過緊，已改為不專任，所有人共同輪替。")
        return forced
    return chosen


def _make_groups(problem: Problem, dedicated: Set[int]) -> List[_Group]:
    all_pos = [p.id for p in problem.positions]
    if not dedicated or len(dedicated) == len(all_pos):
        staff = [
            i for i, s in enumerate(problem.staff) if any(pid in s.qualified for pid in all_pos)
        ]
        return [_Group("全部崗位", all_pos, staff)]
    ded_pos = [pid for pid in all_pos if pid in dedicated]
    rest_pos = [pid for pid in all_pos if pid not in dedicated]
    d_staff = [
        i for i, s in enumerate(problem.staff) if any(pid in s.qualified for pid in ded_pos)
    ]
    d_set = set(d_staff)
    r_staff = [
        i
        for i, s in enumerate(problem.staff)
        if i not in d_set and any(pid in s.qualified for pid in rest_pos)
    ]
    return [_Group("專任組", ded_pos, d_staff), _Group("一般組", rest_pos, r_staff)]


# ---------------------------------------------------------------------------
# 主求解
# ---------------------------------------------------------------------------

def solve(problem: Problem, options: Optional[SolveOptions] = None) -> SolveResult:
    options = options or SolveOptions()
    rng = random.Random(options.seed)
    res = SolveResult(ok=False)

    errs = _validate_problem(problem)
    if errs:
        res.errors = errs
        return res

    pos_by_id = {p.id: p for p in problem.positions}
    sidx = {s.id: i for i, s in enumerate(problem.staff)}
    qual: Dict[int, Set[int]] = {
        p.id: {i for i, s in enumerate(problem.staff) if p.id in s.qualified}
        for p in problem.positions
    }
    qual_count = {pid: len(v) for pid, v in qual.items()}
    S = len(problem.slots)

    # 鎖定資訊
    locked_seats: Dict[SeatKey, int] = {k: sidx[sid] for k, sid in problem.locked.items()}
    pinned: List[Set[int]] = [set() for _ in range(S)]
    for (si, _pid, _k), i in locked_seats.items():
        pinned[si].add(i)

    dedicated = _choose_dedicated(problem, qual_count, res.notes)
    groups = _make_groups(problem, dedicated)

    def feasibility(gs: List[_Group]) -> List[str]:
        out: List[str] = []
        for g in gs:
            for si in range(S):
                seat_pos = [
                    pid
                    for pid in g.pos_ids
                    for k in range(pos_by_id[pid].headcount)
                    if (si, pid, k) not in locked_seats
                ]
                pool = [i for i in g.staff_idx if i not in pinned[si]]
                _m, info = _perfect_match(seat_pos, pool, qual)
                if info is not None:
                    seats, persons = info
                    names = sorted({pos_by_id[seat_pos[u]].name for u in seats})
                    slot = problem.slots[si]
                    out.append(
                        f"{_day_label(problem, slot.day)} {slot.label}：崗位「{'、'.join(names)}」"
                        f"合計需要 {len(seats)} 人，但可安排且具備資格的人員只有 {len(persons)} 位，無法補滿"
                    )
                    break  # 每組回報第一個問題班次即可
        return out

    errs = feasibility(groups)
    if errs and len(groups) > 1:
        res.warnings.append("專任規則在本次人力配置下無法排出完整班表，已放寬為所有人共同輪替各崗位。")
        all_pos = [p.id for p in problem.positions]
        groups = [
            _Group(
                "全部崗位",
                all_pos,
                [i for i, s in enumerate(problem.staff) if any(pid in s.qualified for pid in all_pos)],
            )
        ]
        errs = feasibility(groups)
    if errs:
        res.errors = errs
        return res

    if len(groups) > 1:
        for g in groups:
            names = "、".join(problem.staff[i].name for i in g.staff_idx)
            pos_names = "、".join(pos_by_id[pid].name for pid in g.pos_ids)
            res.notes.append(f"{g.name}（{pos_names}）：{names}")

    grouped = {i for g in groups for i in g.staff_idx}
    idle = [problem.staff[i].name for i in range(len(problem.staff)) if i not in grouped]
    if idle:
        res.warnings.append(
            "以下人員沒有任何本次所需崗位的資格，不會被排班：" + "、".join(idle)
        )

    # 工作矩陣：W[i][s] = 1 表示人員 i 在班次 s 上班
    W: List[bytearray] = [bytearray(S) for _ in problem.staff]
    for (si, _pid, _k), i in locked_seats.items():
        W[i][si] = 1
    pinned_base = [bytes(w) for w in W]

    deadline = time.perf_counter() + max(0.5, options.time_limit)
    cur_all: List[Optional[List[List[int]]]] = [None] * len(groups)
    for gi, g in enumerate(groups):
        share = (deadline - time.perf_counter()) / (len(groups) - gi)
        cur_all[gi] = _solve_group(
            problem, g, W, pinned_base, locked_seats, pinned, qual, pos_by_id, rng,
            options, time.perf_counter() + share,
        )

    # 把人員分配到座位
    assignments: Dict[SeatKey, int] = {}
    poscount: Dict[Tuple[int, int], int] = defaultdict(int)
    last_pos: Dict[Tuple[int, int], int] = {}  # (人員, 班次) -> 崗位
    for gi, g in enumerate(groups):
        cur = cur_all[gi]
        assert cur is not None
        for si in range(S):
            free_seats = [
                (pid, k)
                for pid in g.pos_ids
                for k in range(pos_by_id[pid].headcount)
                if (si, pid, k) not in locked_seats
            ]
            persons = cur[si]
            if len(persons) != len(free_seats):
                res.errors.append("內部錯誤：班次人數與座位數不一致")
                return res
            n = len(free_seats)
            if n == 0:
                continue
            BIG = 1_000_000.0
            cost = []
            for i in persons:
                row = []
                for pid, _k in free_seats:
                    if pid not in problem.staff[i].qualified:
                        row.append(BIG)
                    else:
                        c = 10.0 * poscount[(i, pid)]
                        if last_pos.get((i, si - 1)) == pid:
                            c += 4.0
                        row.append(c + rng.random())
                cost.append(row)
            ans = _hungarian(cost)
            for r, c in enumerate(ans):
                i = persons[r]
                pid, k = free_seats[c]
                if cost[r][c] >= BIG:
                    res.errors.append("內部錯誤：座位配對失敗")
                    return res
                assignments[(si, pid, k)] = problem.staff[i].id
                poscount[(i, pid)] += 1
                last_pos[(i, si)] = pid
    for key, sid in problem.locked.items():
        assignments[key] = sid

    res.assignments = assignments
    res.ok = True

    # 事後分析與提示
    an = analyze(problem, assignments)
    res.warnings.extend(an.warnings)
    for g in groups:
        members = [problem.staff[i] for i in g.staff_idx]
        if not members:
            continue
        cs = [an.counts[m.id] for m in members]
        if max(cs) - min(cs) > 1:
            res.warnings.append(
                f"{g.name}班次數差距較大（最多 {max(cs)} 班、最少 {min(cs)} 班），"
                "多半是部分人員的崗位資格有限所致。"
            )
        seats_per_slot = sum(pos_by_id[pid].headcount for pid in g.pos_ids)
        longest = max(an.max_run[m.id] for m in members)
        if problem.avoid_consecutive and longest >= 2:
            if len(members) < 2 * seats_per_slot:
                res.warnings.append(
                    f"{g.name}人力吃緊（每班需 {seats_per_slot} 人，只有 {len(members)} 人可輪替），"
                    f"無法完全做到 1 休 1，最長連續 {longest} 班。"
                )
            elif longest >= 3:
                res.warnings.append(f"{g.name}仍有連續 {longest} 班的情形，可再按一次「重新排班」。")
    return res


# ---------------------------------------------------------------------------
# 單一組別：模擬退火
# ---------------------------------------------------------------------------

def _solve_group(
    problem: Problem,
    g: _Group,
    W: List[bytearray],
    pinned_base: List[bytes],
    locked_seats: Dict[SeatKey, int],
    pinned: List[Set[int]],
    qual: Dict[int, Set[int]],
    pos_by_id: Dict[int, PositionInfo],
    rng: random.Random,
    options: SolveOptions,
    deadline: float,
) -> List[List[int]]:
    S = len(problem.slots)
    P = list(g.staff_idx)
    run_w = W_RUN if problem.avoid_consecutive else 0.0
    gap_w = W_GAP if problem.avoid_consecutive else 0.0

    # 每班資訊
    seat_pos: List[List[int]] = []
    for si in range(S):
        seat_pos.append(
            [
                pid
                for pid in g.pos_ids
                for k in range(pos_by_id[pid].headcount)
                if (si, pid, k) not in locked_seats
            ]
        )
    sig_ids: Dict[Tuple[int, ...], int] = {}
    sig_of: List[int] = []
    for si in range(S):
        sig = tuple(sorted(seat_pos[si]))
        sig_of.append(sig_ids.setdefault(sig, len(sig_ids)))

    person_pos: Dict[int, Set[int]] = {
        i: {pid for pid in g.pos_ids if i in qual[pid]} for i in P
    }
    elig: List[List[int]] = []
    elig_set: List[Set[int]] = []
    for si in range(S):
        ps = set(seat_pos[si])
        lst = [i for i in P if i not in pinned[si] and person_pos[i] & ps]
        elig.append(lst)
        elig_set.append(set(lst))

    n_at = [len(sp) for sp in seat_pos]
    movable = [si for si in range(S) if n_at[si] > 0]

    # 每天的班次（供成本計算）
    day_slots: Dict[int, List[Tuple[int, int]]] = defaultdict(list)
    for si, sl in enumerate(problem.slots):
        day_slots[sl.day].append((si, sl.tpos))
    slot_day = [sl.day for sl in problem.slots]
    prev_adj: List[Optional[int]] = [None] * S
    for si in range(1, S):
        a, b = problem.slots[si - 1], problem.slots[si]
        if a.day == b.day and a.tpos + 1 == b.tpos:
            prev_adj[si] = si - 1

    def day_cost(i: int, d: int) -> float:
        Wi = W[i]
        run = 0
        last_t = None
        n = 0
        c = 0.0
        for si, t in day_slots[d]:
            if Wi[si]:
                n += 1
                if last_t is not None:
                    gap = t - last_t - 1
                    if gap == 0:
                        run += 1
                    else:
                        c += run * run * run_w
                        run = 0
                        if gap >= 2:
                            c += (gap - 1) * (gap - 1) * gap_w
                last_t = t
        c += run * run * run_w
        c += n * n * W_DAY
        return c

    feas_cache: Dict[Tuple[int, FrozenSet[int]], bool] = {}

    def feasible(si: int, persons: Sequence[int]) -> bool:
        key = (sig_of[si], frozenset(persons))
        v = feas_cache.get(key)
        if v is None:
            v = _perfect_match(seat_pos[si], list(persons), qual)[0] is not None
            feas_cache[key] = v
        return v

    def total_cost(cnt: Dict[int, int]) -> float:
        c = 0.0
        for i in P:
            c += W_COUNT * cnt[i] * cnt[i]
            for d in day_slots:
                c += day_cost(i, d)
        return c

    def construct() -> Tuple[List[List[int]], Dict[int, int]]:
        for i in P:
            W[i][:] = pinned_base[i]
        cnt = {i: sum(W[i]) for i in P}
        cur: List[List[int]] = [[] for _ in range(S)]
        for si in range(S):
            if n_at[si] == 0:
                continue
            pa = prev_adj[si]

            def key_fn(i: int, pa=pa) -> Tuple[float, float]:
                pen = 0.9 if (pa is not None and W[i][pa]) else 0.0
                return (cnt[i] + pen, rng.random())

            pool = sorted(elig[si], key=key_fn)
            m, _info = _perfect_match(seat_pos[si], pool, qual)
            if m is None:  # 理論上不會發生（已先檢查可行性）
                raise RuntimeError("初始排班失敗")
            for i in m:
                W[i][si] = 1
                cnt[i] += 1
            cur[si] = list(m)
        return cur, cnt

    def anneal(cur: List[List[int]], cnt: Dict[int, int], iters: int, t0: float, t1: float,
               stop_at: float) -> float:
        cost = total_cost(cnt)
        if len(movable) == 0 or iters <= 0:
            return cost
        exp = math.exp
        rand = rng.random
        for it in range(iters):
            if (it & 255) == 0 and time.perf_counter() > stop_at:
                break
            T = t0 * (t1 / t0) ** (it / iters)
            if len(movable) >= 2 and rand() < 0.5:
                # 對調：a 把 s 班讓給 b，b 把 t 班讓給 a（總班次數不變）
                s = movable[int(rand() * len(movable))]
                t = movable[int(rand() * len(movable))]
                if s == t:
                    continue
                cs, ct = cur[s], cur[t]
                ia = int(rand() * len(cs))
                ib = int(rand() * len(ct))
                a, b = cs[ia], ct[ib]
                if a == b or W[a][t] or W[b][s]:
                    continue
                if a not in elig_set[t] or b not in elig_set[s]:
                    continue
                ds, dt = slot_day[s], slot_day[t]
                days = (ds,) if ds == dt else (ds, dt)
                old = sum(day_cost(x, d) for x in (a, b) for d in days)
                W[a][s] = 0
                W[b][s] = 1
                W[b][t] = 0
                W[a][t] = 1
                new = sum(day_cost(x, d) for x in (a, b) for d in days)
                delta = new - old
                if delta <= 0 or rand() < exp(-delta / T):
                    ns = cs[:ia] + [b] + cs[ia + 1:]
                    nt = ct[:ib] + [a] + ct[ib + 1:]
                    if feasible(s, ns) and feasible(t, nt):
                        cur[s], cur[t] = ns, nt
                        cost += delta
                        continue
                W[a][s] = 1
                W[b][s] = 0
                W[b][t] = 1
                W[a][t] = 0
            else:
                # 換人：a 在 s 班下班，由閒置的 b 接手
                s = movable[int(rand() * len(movable))]
                cs = cur[s]
                cand = elig[s]
                if not cand:
                    continue
                ia = int(rand() * len(cs))
                a = cs[ia]
                b = cand[int(rand() * len(cand))]
                if W[b][s]:
                    continue
                d = slot_day[s]
                old = (
                    day_cost(a, d) + day_cost(b, d)
                    + W_COUNT * (cnt[a] * cnt[a] + cnt[b] * cnt[b])
                )
                W[a][s] = 0
                W[b][s] = 1
                cnt[a] -= 1
                cnt[b] += 1
                new = (
                    day_cost(a, d) + day_cost(b, d)
                    + W_COUNT * (cnt[a] * cnt[a] + cnt[b] * cnt[b])
                )
                delta = new - old
                if delta <= 0 or rand() < exp(-delta / T):
                    ns = cs[:ia] + [b] + cs[ia + 1:]
                    if feasible(s, ns):
                        cur[s] = ns
                        cost += delta
                        continue
                W[a][s] = 1
                W[b][s] = 0
                cnt[a] += 1
                cnt[b] -= 1
        return cost

    S_seats = sum(n_at)
    iters = min(options.max_iters, max(40000, 150 * max(1, S_seats)))
    best = None  # (cost, cur, W 快照)
    restarts = max(1, options.restarts)
    for r in range(restarts):
        remaining = deadline - time.perf_counter()
        if r > 0 and remaining <= 0:
            break
        stop_at = time.perf_counter() + (remaining / (restarts - r) if remaining > 0 else 0.05)
        cur, cnt = construct()
        anneal(cur, cnt, int(iters * 0.9), 120.0, 3.0, stop_at)
        anneal(cur, cnt, int(iters * 0.1), 1.0, 0.01, stop_at + 0.2)
        c = total_cost(cnt)
        if best is None or c < best[0]:
            best = (c, [list(x) for x in cur], {i: bytes(W[i]) for i in P})
    assert best is not None
    for i in P:
        W[i][:] = best[2][i]
    return best[1]
