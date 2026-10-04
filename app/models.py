from dataclasses import dataclass
from enum import StrEnum
from typing import List


class CredentialType(StrEnum):
    CODEX_AUTH = "codex_auth"
    RELAY_API = "relay_api"


class LoadStrategy(StrEnum):
    NORMAL = "normal"
    PRIORITY = "priority"
    DISABLED = "disabled"


class CloudSyncAction(StrEnum):
    REFRESH = "refresh"
    UPLOAD = "upload"
    PULL = "pull"
    DELETE = "delete"


class ProxyKillReason(StrEnum):
    NONE = ""
    QUOTA_DROP = "quota_drop"
    TOKEN_MISMATCH = "token_mismatch"


class ProxyControlMessage(StrEnum):
    AUTH = "AUTH"
    USED = "USED"
    DISCOVERY = "DISCOVERY"
    RESELECT = "RESELECT"
    PINGPONG = "PINGPONG"
    MANUAL_KILL = "MANUAL_KILL"
    IDLE_TIMEOUT = "IDLE_TIMEOUT"
    KILL_RESULT = "KILL_RESULT"
    MANUAL_KILL_RESULT = "MANUAL_KILL_RESULT"
    TRAFFIC = "TRAFFIC"
    TOKEN_SPEED = "TOKEN_SPEED"


PROXY_ACK_OK = "OK"
PROXY_ACK_YES = "1"
PROXY_ACK_NO = "0"


class ResetCreditResult(StrEnum):
    RESET = "reset"
    NOTHING_TO_RESET = "nothing_to_reset"
    NO_CREDIT = "no_credit"
    ALREADY_REDEEMED = "already_redeemed"


CREDENTIAL_TYPE_CODEX_AUTH = CredentialType.CODEX_AUTH
CREDENTIAL_TYPE_RELAY_API = CredentialType.RELAY_API


@dataclass
class RelayConfig:
    credential_id: str
    name: str
    base_url: str
    api_key: str
    model: str = ""
    note: str = ""
    file_name: str = ""


@dataclass
class UserProfile:
    user_id: str = "-"
    user_name: str = "-"
    user_email: str = "-"
    expire_time: str = "-"


@dataclass
class AccountToken:
    account_id: str
    plan_type: str
    structure: str
    access_token: str
    session_token: str


@dataclass
class SessionViewData:
    profile: UserProfile
    accounts: List[AccountToken]


@dataclass
class SessionFetchResult:
    view_data: SessionViewData
    message: str = ""


@dataclass(frozen=True, slots=True)
class ResetCredit:
    credit_id: str
    granted_at: float
    expires_at: float | None = None


@dataclass(frozen=True, slots=True)
class ResetCreditInfo:
    available_count: int
    expiry_times: tuple[str, ...] = ()
    expiry_timestamps: tuple[float, ...] = ()
    credits: tuple[ResetCredit, ...] = ()


@dataclass(frozen=True, slots=True)
class ResetCreditState:
    info: ResetCreditInfo | None = None
    failed: bool = False
