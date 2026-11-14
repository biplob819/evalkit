"""The live-mode guard (D3.1, CMP-04/05).

Before any model call a live request must pass, in order: the passcode (constant-time
compare), the session's rate limit, the per-request cost cap, and the daily spend cap. The
daily cap is enforced by reserving the request's exact upper bound (the most it can spend:
every call stops at its output limit and the executor enforces the bound) with one atomic
UPDATE in a shared database (Postgres in production, D-53; SQLite for local use), so
concurrent requests can never spend past it together and the counter survives app restarts.
After the call the reservation is settled to the actual cost.
"""

from __future__ import annotations

import hmac
import sqlite3
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

DAILY_CAP_USD = 3.0  # FR16
RATE_LIMIT = 5  # live requests per session
RATE_WINDOW_S = 3600.0
MAX_SESSIONS = 10_000  # rate-limit memory bound: the oldest idle sessions are forgotten first


class GuardRefused(Exception):
    """A live request refused before any model call. `reason` is safe to show the visitor."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SpendStore(Protocol):
    def reserve(self, day: str, amount: float, cap: float) -> bool: ...
    def settle(self, day: str, reserved: float, actual: float) -> None: ...
    def spent(self, day: str) -> float: ...


class SqliteSpend:
    """Daily spend in a local SQLite file (development and tests)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS demo_spend (day TEXT PRIMARY KEY, usd REAL NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)

    def reserve(self, day: str, amount: float, cap: float) -> bool:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")  # one writer at a time: check and add together
            con.execute("INSERT OR IGNORE INTO demo_spend (day, usd) VALUES (?, 0)", (day,))
            cur = con.execute(
                "UPDATE demo_spend SET usd = usd + ? WHERE day = ? AND usd + ? <= ?",
                (amount, day, amount, cap),
            )
            con.execute("COMMIT")
            return cur.rowcount == 1
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def settle(self, day: str, reserved: float, actual: float) -> None:
        con = self._connect()
        try:
            con.execute(
                "UPDATE demo_spend SET usd = MAX(0, usd + ?) WHERE day = ?",
                (actual - reserved, day),
            )
        finally:
            con.close()

    def spent(self, day: str) -> float:
        con = self._connect()
        try:
            row = con.execute("SELECT usd FROM demo_spend WHERE day = ?", (day,)).fetchone()
            return float(row[0]) if row else 0.0
        finally:
            con.close()


def _dec(x: float) -> Decimal:
    """Money as an exact decimal for the NUMERIC column (6 places: a millionth of a dollar)."""
    return Decimal(str(round(x, 6)))


class PostgresSpend:
    """Daily spend in Postgres (D-53): one row per UTC day, reserved with one atomic UPDATE.
    Needs the `demo` extra (psycopg). Connection string from `EVALKIT_DEMO_DATABASE_URL`."""

    TABLE = (
        "CREATE TABLE IF NOT EXISTS demo_spend (day DATE PRIMARY KEY, "
        "usd NUMERIC(12, 6) NOT NULL DEFAULT 0)"  # exact cents: no float drift at the cap
    )

    def __init__(self, url: str, connect: Callable[[str], Any] | None = None) -> None:
        if connect is None:
            import psycopg

            connect = psycopg.connect
        self._connect = lambda: connect(url)
        try:
            with self._connect() as con:
                con.execute(self.TABLE)
        except Exception as exc:  # two processes creating the table at once: one wins
            if type(exc).__name__ not in ("UniqueViolation", "DuplicateTable"):
                raise

    def reserve(self, day: str, amount: float, cap: float) -> bool:
        with self._connect() as con:
            con.execute(
                "INSERT INTO demo_spend (day, usd) VALUES (%s, 0) ON CONFLICT (day) DO NOTHING",
                (day,),
            )
            row = con.execute(
                "UPDATE demo_spend SET usd = usd + %s WHERE day = %s AND usd + %s <= %s "
                "RETURNING usd",
                (_dec(amount), day, _dec(amount), _dec(cap)),
            ).fetchone()
            return row is not None

    def settle(self, day: str, reserved: float, actual: float) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE demo_spend SET usd = GREATEST(0, usd + %s) WHERE day = %s",
                (_dec(actual) - _dec(reserved), day),
            )

    def spent(self, day: str) -> float:
        with self._connect() as con:
            row = con.execute("SELECT usd FROM demo_spend WHERE day = %s", (day,)).fetchone()
            return float(row[0]) if row else 0.0


@dataclass(frozen=True)
class Ticket:
    """A reserved live request: settle it with the actual cost."""

    day: str
    reserved: float


class Guard:
    def __init__(
        self,
        passcode: str,
        spend: SpendStore,
        *,
        per_request_cap: float,
        daily_cap: float = DAILY_CAP_USD,
        rate_limit: int = RATE_LIMIT,
        rate_window_s: float = RATE_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], str] = lambda: datetime.now(UTC).date().isoformat(),
    ) -> None:
        if not passcode:
            raise ValueError("live mode needs a passcode")
        if per_request_cap <= 0 or daily_cap <= 0:
            raise ValueError("caps must be positive")
        self._passcode = passcode.encode()
        self.spend = spend
        self.per_request_cap = per_request_cap
        self.daily_cap = daily_cap
        self.rate_limit = rate_limit
        self.rate_window_s = rate_window_s
        self._clock = clock
        self._today = today
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check_passcode(self, passcode: str) -> None:
        """First gate, before any outside call (even the PubMed fetch)."""
        if not hmac.compare_digest(passcode.encode(), self._passcode):
            raise GuardRefused("Wrong passcode.")

    def reserve(self, passcode: str, session: str, hold_usd: float) -> Ticket:
        """Refuse (GuardRefused) or reserve `hold_usd`, the request's upper bound, against
        today's budget; nothing is called before this returns. Settle with the actual cost."""
        self.check_passcode(passcode)
        if not session:
            raise GuardRefused("No session.")
        if hold_usd > self.per_request_cap:
            raise GuardRefused(
                f"This request could cost up to ${hold_usd:.2f}, over the "
                f"${self.per_request_cap:.2f} per-request limit. Try a shorter abstract."
            )
        with self._lock:  # tentatively take a rate-limit slot
            now = self._clock()
            self._evict(now)
            hits = self._hits[session]
            while hits and now - hits[0] > self.rate_window_s:
                hits.popleft()
            if len(hits) >= self.rate_limit:
                raise GuardRefused(
                    f"Rate limit: {self.rate_limit} live requests per "
                    f"{self.rate_window_s / 60:.0f} minutes. Replay mode is always available."
                )
            hits.append(now)
        day = self._today()
        # The bound is what the request may spend at most, so that is what is held against
        # today's budget; settle() returns the unused part.
        if not self.spend.reserve(day, hold_usd, self.daily_cap):
            with self._lock:  # give the slot back: nothing ran
                if self._hits.get(session) and self._hits[session][-1] == now:
                    self._hits[session].pop()
            raise GuardRefused("Today's live budget is used up. Replay mode is always available.")
        return Ticket(day=day, reserved=hold_usd)

    def _evict(self, now: float) -> None:
        if len(self._hits) <= MAX_SESSIONS:
            return
        for key in [k for k, v in self._hits.items() if not v or now - v[-1] > self.rate_window_s]:
            del self._hits[key]
        while len(self._hits) > MAX_SESSIONS:  # still too many active: drop the oldest
            del self._hits[min(self._hits, key=lambda k: self._hits[k][-1])]

    def settle(self, ticket: Ticket, actual_usd: float) -> None:
        self.spend.settle(ticket.day, ticket.reserved, max(0.0, actual_usd))
