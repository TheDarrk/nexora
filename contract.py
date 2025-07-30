from near_sdk_py import (
    Contract,
    init,
    call,
    view,
    ONE_NEAR
)
from near_sdk_py.promises import Promise
from typing import Dict, List, Optional

# ─────────────────────────── CONSTANTS ────────────────────────────
EARLY_BIRD_RATE      = 32                   # pts / Ⓝ before timer
POINT_DECAY_RATES    = [24, 23, 22, 21, 20, 19, 18, 17, 16, 15]  # per-hour
FIRST_WINDOW_HOURS   = 3                   # 0–3 h  → 60–90 %
SECOND_WINDOW_HOURS  = 6                   # 3–6 h  → 20–40 %
MAX_THROWS_PER_GAME  = 2                   # per-player quota

# ─────────────────────────── CONTRACT ─────────────────────────────
class TeamBettingContract(Contract):
    # ── INITIALISATION ────────────────────────────────────────────
    @init
    def initialize(self, admin_id: str, timer_bot_id: Optional[str] = None):
        self.storage["admin"]               = admin_id
        self.storage["timer_bot"]           = timer_bot_id or ""
        self.storage["paused"]              = False

        # game lifecycle
        self.storage["game_active"]         = False
        self.storage["game_started"]        = False
        self.storage["game_start_time"]     = 0

        # parameters
        self.storage["pot_size"]            = 0        # Ⓝ
        self.storage["commission_rate"]     = 10       # %
        self.storage["game_duration"]       = 3600     # seconds

        # dynamic state
        self.storage["force_refund_mode"]   = False
        self.storage["winning_team"]        = ""
        self.storage["withdrawable"]        = {}

        # betting state
        self.storage["team_a_bets"]         = {}
        self.storage["team_b_bets"]         = {}
        self.storage["team_a_points"]       = 0
        self.storage["team_b_points"]       = 0
        self.storage["team_a_total_amount"] = 0
        self.storage["team_b_total_amount"] = 0

        # moderation
        self.storage["banned_players"]      = {}

        # transfer mechanic
        self.storage["transfer_counts"]     = {}   # {player → int}
        self.storage["last_transfer_time"]  = {}   # {player → ns}

        # decay schedule
        self.storage["point_rates"]         = POINT_DECAY_RATES

    # ── ASSERTS ──────────────────────────────────────────────────
    def _assert_admin(self):
        if self.predecessor_account_id != self.storage["admin"]:
            raise Exception("Only admin")

    def _assert_timer_or_admin(self):
        if self.predecessor_account_id not in (
            self.storage["admin"], self.storage.get("timer_bot", "")
        ):
            raise Exception("Only admin or timer bot")

    def _assert_not_paused(self):
        if self.storage["paused"]:
            raise Exception("Contract is paused")

    def _assert_not_banned(self, user: str):
        if self.storage["banned_players"].get(user, False):
            raise Exception("Player is banned")

    def _assert_game_active(self):
        if not self.storage["game_active"]:
            raise Exception("No active game")

    def _assert_game_started(self):
        if not self.storage["game_started"]:
            raise Exception("Timer not started")

    # ── ADMIN CONTROLS ──────────────────────────────────────────
    @call
    def pause_game(self):
        self._assert_admin()
        self.storage["paused"] = True
        self.log_event("game_paused", {"by": self.predecessor_account_id})

    @call
    def unpause_game(self):
        self._assert_admin()
        self.storage["paused"] = False
        self.log_event("game_unpaused", {"by": self.predecessor_account_id})

    @call
    def set_game_duration(self, duration_seconds: int):
        self._assert_admin(); self._assert_not_paused()
        if self.storage["game_active"]:
            raise Exception("Cannot change during active game")
        if duration_seconds < 60:
            raise Exception("Duration < 60 s")
        self.storage["game_duration"] = duration_seconds
        self.log_event("duration_set", {"sec": duration_seconds})

    @call
    def set_pot_size(self, pot_near: int):
        self._assert_admin(); self._assert_not_paused()
        if self.storage["game_active"]:
            raise Exception("Cannot change during active game")
        self.storage["pot_size"] = pot_near
        self.log_event("pot_set", {"near": pot_near})

    @call
    def set_commission_rate(self, rate: int):
        self._assert_admin(); self._assert_not_paused()
        if self.storage["game_active"]:
            raise Exception("Cannot change during active game")
        if not 0 <= rate <= 50:
            raise Exception("Rate must be 0–50 %")
        self.storage["commission_rate"] = rate
        self.log_event("commission_set", {"rate": rate})

    # ── BAN / UNBAN ─────────────────────────────────────────────
    @call
    def ban_player(self, player_id: str):
        self._assert_admin(); self._assert_not_paused()
        self.storage["banned_players"][player_id] = True
        self.log_event("player_banned", {"player": player_id})

    @call
    def unban_player(self, player_id: str):
        self._assert_admin(); self._assert_not_paused()
        self.storage["banned_players"][player_id] = False
        self.log_event("player_unbanned", {"player": player_id})

    # ── GAME OPEN / TIMER ──────────────────────────────────────
    @call
    def start_game(self):
        self._assert_admin(); self._assert_not_paused()
        if self.storage["game_active"]:
            raise Exception("Already active")
        if self.storage["pot_size"] <= 0:
            raise Exception("Set pot size first")

        # reset state
        self.storage.update({
            "game_active": True,
            "game_started": False,
            "game_start_time": 0,
            "force_refund_mode": False,
            "team_a_bets": {}, "team_b_bets": {},
            "team_a_points": 0, "team_b_points": 0,
            "team_a_total_amount": 0, "team_b_total_amount": 0,
            "winning_team": "",
            "withdrawable": {},
            "transfer_counts": {},
            "last_transfer_time": {},
        })
        self.log_event("game_opened", {
            "pot": self.storage["pot_size"],
            "commission": self.storage["commission_rate"],
            "early_rate": EARLY_BIRD_RATE
        })

    @call
    def start_timer(self):
        self._assert_admin(); self._assert_not_paused(); self._assert_game_active()
        if self.storage["game_started"]:
            raise Exception("Timer already started")
        self._start_timer_internal("manual")

    # INTERNAL
    def _maybe_auto_start_timer(self):
        if self.storage["game_started"]:
            return
        pot = self.storage["pot_size"]
        comm = self.storage["commission_rate"]
        thresh = (pot * (100 + comm) // 100) * ONE_NEAR
        if (self.storage["team_a_total_amount"] >= thresh and
            self.storage["team_b_total_amount"] >= thresh):
            self._start_timer_internal("auto")

    def _start_timer_internal(self, mode: str):
        self.storage["game_started"]    = True
        self.storage["game_start_time"] = self.block_timestamp
        self.log_event("timer_started", {
            "mode": mode,
            "start": self.block_timestamp,
            "duration": self.storage["game_duration"]
        })

    # ── BETTING ─────────────────────────────────────────────────
    @call
    def bet_on_team(self, team: str):
        self._assert_not_paused(); self._assert_game_active()
        if self.storage["force_refund_mode"]:
            raise Exception("Refund mode")
        if team not in ("A", "B"):
            raise Exception("Team must be A or B")
        user = self.predecessor_account_id
        self._assert_not_banned(user)

        if self.attached_deposit < ONE_NEAR // 2:
            raise Exception("Min 0.5Ⓝ")

        # rate
        if not self.storage["game_started"]:
            rate = EARLY_BIRD_RATE
        else:
            elapsed = (self.block_timestamp - self.storage["game_start_time"]) // (
                60 * 60 * 1_000_000_000
            )
            rate = self.storage["point_rates"][int(elapsed)] if elapsed < len(
                self.storage["point_rates"]) else 1
        pts = (self.attached_deposit // ONE_NEAR) * rate

        bets_key   = f"team_{team.lower()}_bets"
        points_key = f"team_{team.lower()}_points"
        total_key  = f"team_{team.lower()}_total_amount"

        bets = self.storage[bets_key]
        if user in bets:
            bets[user]["amount"] += self.attached_deposit
            bets[user]["points"] += pts
        else:
            bets[user] = {"amount": self.attached_deposit, "points": pts}
        self.storage[points_key] += pts
        self.storage[total_key]  += self.attached_deposit

        self.log_event("bet_placed", {
            "user": user, "team": team,
            "yocto": self.attached_deposit, "points": pts, "rate": rate
        })
        self._maybe_auto_start_timer()

    # ── POINT THROW ────────────────────────────────────────────
    @call
    def throw_points(self, percent: int):
        """
        Sacrifice own points to opponent team.
        0–3 h : 60–90 %, 3–6 h : 20–40 %, >6 h : disabled
        Max two throws per game.
        """
        self._assert_not_paused(); self._assert_game_started()
        user = self.predecessor_account_id
        self._assert_not_banned(user)

        counts = self.storage["transfer_counts"]
        if counts.get(user, 0) >= MAX_THROWS_PER_GAME:
            raise Exception("Limit reached")

        elapsed = (self.block_timestamp - self.storage["game_start_time"]) // (
            60 * 60 * 1_000_000_000
        )
        if elapsed < FIRST_WINDOW_HOURS:
            lo, hi = 60, 90
        elif elapsed < SECOND_WINDOW_HOURS:
            lo, hi = 20, 40
        else:
            raise Exception("Window closed")
        if not (lo <= percent <= hi):
            raise Exception(f"Allowed {lo}–{hi}%")

        if user in self.storage["team_a_bets"]:
            donor_key, recip_key = "team_a_bets", "team_b_bets"
            donor_pts_key, recip_pts_key = "team_a_points", "team_b_points"
            donor_team, recip_team = "A", "B"
        elif user in self.storage["team_b_bets"]:
            donor_key, recip_key = "team_b_bets", "team_a_bets"
            donor_pts_key, recip_pts_key = "team_b_points", "team_a_points"
            donor_team, recip_team = "B", "A"
        else:
            raise Exception("No bet")

        donor_bets = self.storage[donor_key]
        rec_bets   = self.storage[recip_key]  # only to guarantee existence
        record = donor_bets[user]
        pts_owned = record["points"]
        pts_move  = (pts_owned * percent) // 100
        if pts_move <= 0 or pts_owned - pts_move < 1:
            raise Exception("Invalid amount")

        record["points"] -= pts_move
        donor_bets[user] = record
        self.storage[donor_key] = donor_bets

        self.storage[donor_pts_key] -= pts_move
        self.storage[recip_pts_key] += pts_move

        counts[user] = counts.get(user, 0) + 1
        self.storage["transfer_counts"] = counts
        self.storage["last_transfer_time"][user] = self.block_timestamp
        self.log_event("points_transferred", {
            "user": user, "from": donor_team, "to": recip_team,
            "pct": percent, "pts": pts_move, "used": counts[user]
        })

    # ── FORCE REFUND ────────────────────────────────────────────
    @call
    def force_end_game_refund(self):
        self._assert_admin(); self._assert_not_paused(); self._assert_game_active()
        self.storage["game_active"] = False
        self.storage["force_refund_mode"] = True

        withdraw = self.storage["withdrawable"]
        refunded = 0
        for team_key, label in (("team_a_bets", "A"), ("team_b_bets", "B")):
            for uid, b in self.storage[team_key].items():
                amt = b["amount"]
                refunded += amt
                withdraw[uid] = withdraw.get(uid, 0) + amt
                self.log_event("force_refund", {
                    "user": uid, "team": label,
                    "refund": amt, "original": b["amount"]
                })
        self.storage["withdrawable"] = withdraw
        self.log_event("game_force_ended", {
            "admin": self.predecessor_account_id,
            "total_refunded": refunded
        })

    # ── END GAME & PAYOUT ──────────────────────────────────────
    @call
    def end_game(self):
        self._assert_timer_or_admin(); self._assert_game_started()
        a_pts = self.storage["team_a_points"]
        b_pts = self.storage["team_b_points"]
        if a_pts == b_pts:
            raise Exception("Tie – refund or extend")

        self.storage["winning_team"] = "A" if a_pts < b_pts else "B"
        self.storage["game_active"]  = False
        self.log_event("game_ended", {
            "winner": self.storage["winning_team"],
            "team_a_pts": a_pts, "team_b_pts": b_pts
        })
        self._distribute_payouts()

    def _distribute_payouts(self):
        win = self.storage["winning_team"]
        pot = self.storage["pot_size"] * ONE_NEAR
        comm_pct = self.storage["commission_rate"]

        win_key  = f"team_{win.lower()}_bets"
        lose_key = f"team_{'a' if win == 'B' else 'b'}_bets"

        win_bets  = self.storage[win_key]
        lose_bets = self.storage[lose_key]

        win_total_amt = sum(b["amount"] for b in win_bets.values())
        win_total_pts = sum(b["points"] for b in win_bets.values())
        lose_total_amt= sum(b["amount"] for b in lose_bets.values())

        commission = (pot * comm_pct) // 100
        total_to_pay = pot + commission
        withdraw = self.storage["withdrawable"]

        for uid, b in win_bets.items():
            share = (b["points"] * pot) // win_total_pts if win_total_pts else 0
            payout = b["amount"] + share
            withdraw[uid] = withdraw.get(uid, 0) + payout
            self.log_event("winner_payout", {
                "user": uid, "bet": b["amount"], "share": share, "payout": payout
            })

        if lose_total_amt >= total_to_pay:
            for uid, b in lose_bets.items():
                loss = (b["amount"] * total_to_pay) // lose_total_amt
                refund = b["amount"] - loss
                withdraw[uid] = withdraw.get(uid, 0) + refund
                self.log_event("loser_refund", {
                    "user": uid, "bet": b["amount"], "refund": refund
                })
        self.storage["withdrawable"] = withdraw
        admin = self.storage["admin"]
        withdraw[admin] = withdraw.get(admin, 0) + commission
        self.storage["withdrawable"] = withdraw
        self.log_event("commission_recorded", {
            "admin": admin, "commission": commission
        })

    # ── WITHDRAW ───────────────────────────────────────────────
    @call
    def withdraw(self):
        self._assert_not_paused()
        if self.storage["game_active"]:
            raise Exception("Game active")
        uid = self.predecessor_account_id
        amt = self.storage["withdrawable"].get(uid, 0)
        if amt == 0:
            raise Exception("Nothing to withdraw")
        Promise.create_batch(uid).transfer(amt)
        self.storage["withdrawable"][uid] = 0
        self.log_event("withdraw", {"user": uid, "yocto": amt})

    # ── VIEW METHODS ───────────────────────────────────────────
    @view
    def get_game_status(self) -> Dict:
        return {
            "active": self.storage["game_active"],
            "started": self.storage["game_started"],
            "paused": self.storage["paused"],
            "force_refund_mode": self.storage["force_refund_mode"],
            "start_time": self.storage["game_start_time"],
            "duration": self.storage["game_duration"],
            "pot_near": self.storage["pot_size"],
            "commission_rate": self.storage["commission_rate"],
            "team_a_points": self.storage["team_a_points"],
            "team_b_points": self.storage["team_b_points"],
            "team_a_total": self.storage["team_a_total_amount"],
            "team_b_total": self.storage["team_b_total_amount"],
            "winning_team": self.storage["winning_team"]
        }

    @view
    def get_team_bets(self, team: str) -> Dict:
        if team not in ("A", "B"): return {}
        return self.storage[f"team_{team.lower()}_bets"]

    @view
    def get_user_bet(self, user_id: str, team: str) -> Dict:
        if team not in ("A", "B"): return {}
        return self.storage[f"team_{team.lower()}_bets"].get(user_id, {})

    @view
    def calculate_current_points(self, amount_near: int) -> int:
        if not self.storage["game_active"]: return 0
        if not self.storage["game_started"]:
            return amount_near * EARLY_BIRD_RATE
        elapsed = (self.block_timestamp - self.storage["game_start_time"]) // (
            60 * 60 * 1_000_000_000
        )
        rate = self.storage["point_rates"][int(elapsed)] if elapsed < len(
            self.storage["point_rates"]) else 1
        return amount_near * rate

    @view
    def get_admin_info(self) -> Dict:
        return {
            "admin": self.storage["admin"],
            "paused": self.storage["paused"],
            "pot_near": self.storage["pot_size"],
            "commission_rate": self.storage["commission_rate"],
            "duration": self.storage["game_duration"]
        }

    @view
    def is_player_banned(self, player_id: str) -> bool:
        return self.storage["banned_players"].get(player_id, False)

    @view
    def get_banned_players(self) -> List[str]:
        return [uid for uid, banned in self.storage["banned_players"].items() if banned]
