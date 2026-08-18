from dataclasses import dataclass
from typing import List


CREDENTIAL_TYPE_CODEX_AUTH = "codex_auth"
CREDENTIAL_TYPE_RELAY_API = "relay_api"


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
