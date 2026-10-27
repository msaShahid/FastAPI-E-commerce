"""The email services an administrator can choose from. Presets only, by design: the SMTP host and port come from this table, never
from user input, so an administrator account cannot be used to make the server connect to an arbitrary address.

Hosts and ports are the providers' published SMTP settings as far as I know them; confirm them against the provider's current
documentation when you set up an account (they are in one place, here, so a correction is a one-line change)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    label: str
    security: str                                   # starttls | ssl
    hosts: dict[str | None, str]                    # mode -> SMTP host (None = the provider has no modes)
    ports: dict[str | None, int]
    mode_label: str | None = None
    mode_options: dict[str, str] | None = None      # mode -> description
    username_label: str = "Username"
    password_label: str = "Password"
    username_default: dict[str | None, str] | None = None
    delivers_to_real_recipients: dict[str | None, bool] | None = None   # per mode; None = yes
    notes: tuple[str, ...] = ()

    def modes(self) -> list[str | None]:
        return list(self.mode_options) if self.mode_options else [None]

    def host(self, mode: str | None) -> str:
        return self.hosts[mode]

    def port(self, mode: str | None) -> int:
        return self.ports[mode]

    def delivers(self, mode: str | None) -> bool:
        return True if self.delivers_to_real_recipients is None else self.delivers_to_real_recipients.get(mode, True)


PROVIDERS: dict[str, ProviderSpec] = {
    "google": ProviderSpec(
        key="google",
        label="Google (Gmail / Google Workspace)",
        security="starttls",
        hosts={None: "smtp.gmail.com"},
        ports={None: 587},
        username_label="Google account email",
        password_label="App password",
        notes=(
            "Google does not accept your normal account password for SMTP. Turn on 2-Step Verification, then create an App "
            "password in your Google Account and paste it here.",
            "The From address must be the account's own address or an alias you have verified in Gmail; otherwise Google rewrites it.",
            "Gmail applies daily sending limits to accounts: check Google's current numbers before relying on it for order volume.",
        ),
    ),
    "zoho": ProviderSpec(
        key="zoho",
        label="Zoho Mail",
        security="starttls",
        hosts={"com": "smtp.zoho.com", "in": "smtp.zoho.in", "eu": "smtp.zoho.eu", "com.au": "smtp.zoho.com.au", "jp": "smtp.zoho.jp"},
        ports={m: 587 for m in ("com", "in", "eu", "com.au", "jp")},
        mode_label="Data centre",
        mode_options={
            "com": "United States (zoho.com)", "in": "India (zoho.in)", "eu": "Europe (zoho.eu)",
            "com.au": "Australia (zoho.com.au)", "jp": "Japan (zoho.jp)",
        },
        username_label="Zoho Mail address",
        password_label="Password or app-specific password",
        notes=(
            "Pick the data centre your Zoho account lives in (an Indian account normally uses zoho.in); a wrong one fails to sign in.",
            "If Zoho two-factor authentication is on, create an app-specific password in your Zoho account security settings.",
            "The From address must be the Zoho mailbox itself or an alias it owns.",
        ),
    ),
    "mailtrap": ProviderSpec(
        key="mailtrap",
        label="Mailtrap",
        security="starttls",
        hosts={"sandbox": "sandbox.smtp.mailtrap.io", "live": "live.smtp.mailtrap.io"},
        ports={"sandbox": 2525, "live": 587},
        mode_label="Mailtrap product",
        mode_options={
            "sandbox": "Email Testing (sandbox): captures messages in a Mailtrap inbox; NOTHING is delivered to customers",
            "live": "Email Sending: delivers to real recipients from a verified sending domain",
        },
        username_label="SMTP username",
        password_label="SMTP password or API token",
        username_default={"sandbox": "", "live": "api"},
        delivers_to_real_recipients={"sandbox": False, "live": True},
        notes=(
            "Sandbox: copy the username and password from your Mailtrap inbox's SMTP settings. Ideal for development, demos and staging.",
            "Live: the username is normally 'api' and the password is your sending API token; the sending domain must be verified in Mailtrap.",
        ),
    ),
}
