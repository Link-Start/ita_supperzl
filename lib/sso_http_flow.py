# Copyright (c) 2026 Idp Team Automation.
# iDP 协议作者：@该隐；注册机作者：@朴圣佑。
# 二开请保留版权；二开不保留版权，以后写代码都是bug。

"""Pure HTTP SSO/Codex OAuth flow.

The implementation intentionally avoids browser automation.  It follows HTTP
redirects, submits ordinary HTML forms, and records artifacts when the live page
requires an unsupported interactive/JavaScript branch.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .codex_oauth import OAuthStart, TOKEN_URL, parse_callback_url, token_config_from_response
from .errors import OAuthFlowError
from .idp_client import GeneratedAccount
from .logging_utils import JsonlLogger, redact

try:  # pragma: no cover - live dependency is covered by fakes in tests
    from curl_cffi.requests import Session as CurlSession
except Exception:  # pragma: no cover
    CurlSession = None  # type: ignore


@dataclass
class HttpResult:
    status_code: int
    url: str
    headers: dict[str, str]
    text: str = ""
    json_data: Any = None

    def header(self, name: str) -> str:
        lname = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lname:
                return str(value or "")
        return ""


@dataclass
class HtmlForm:
    action: str = ""
    method: str = "GET"
    fields: dict[str, str] = field(default_factory=dict)
    submit_name: str = ""
    submit_value: str = ""


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: list[HtmlForm] = []
        self._current: HtmlForm | None = None
        self._select_name: str = ""
        self._select_first_value: str = ""
        self._select_selected_value: str = ""
        self.links: list[str] = []
        self.meta_refresh: str = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): (v or "") for k, v in attrs}
        tag = tag.lower()
        if tag == "form":
            self._current = HtmlForm(action=html.unescape(attr.get("action", "")), method=(attr.get("method") or "GET").upper())
        elif tag == "input" and self._current is not None:
            name = html.unescape(attr.get("name", ""))
            if not name:
                return
            typ = (attr.get("type") or "text").lower()
            value = html.unescape(attr.get("value", ""))
            if typ in {"submit", "button"}:
                if not self._current.submit_name:
                    self._current.submit_name = name
                    self._current.submit_value = value
            elif typ not in {"checkbox", "radio"} or attr.get("checked") is not None:
                self._current.fields[name] = value
        elif tag == "button" and self._current is not None:
            name = html.unescape(attr.get("name", ""))
            if name and not self._current.submit_name:
                self._current.submit_name = name
                self._current.submit_value = html.unescape(attr.get("value", ""))
        elif tag == "select" and self._current is not None:
            self._select_name = html.unescape(attr.get("name", ""))
            self._select_first_value = ""
            self._select_selected_value = ""
        elif tag == "option" and self._current is not None and self._select_name:
            value = html.unescape(attr.get("value", ""))
            if not self._select_first_value:
                self._select_first_value = value
            if attr.get("selected") is not None:
                self._select_selected_value = value
        elif tag == "a":
            href = html.unescape(attr.get("href", ""))
            if href:
                self.links.append(href)
        elif tag == "meta":
            equiv = attr.get("http-equiv", "").lower()
            content = attr.get("content", "")
            if equiv == "refresh":
                m = re.search(r"url\s*=\s*([^;]+)$", content, re.I)
                if m:
                    self.meta_refresh = html.unescape(m.group(1).strip().strip('"\''))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "select" and self._current is not None and self._select_name:
            self._current.fields[self._select_name] = self._select_selected_value or self._select_first_value
            self._select_name = ""
            self._select_first_value = ""
            self._select_selected_value = ""
        if tag == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None

    def close(self) -> None:
        super().close()
        if self._current is not None:
            self.forms.append(self._current)
            self._current = None


def parse_html_forms(text: str) -> tuple[list[HtmlForm], list[str], str]:
    parser = _FormParser()
    parser.feed(text or "")
    parser.close()
    return parser.forms, parser.links, parser.meta_refresh


def _looks_like_email_field(name: str) -> bool:
    n = name.lower()
    return any(marker in n for marker in ("email", "username", "login", "user", "identifier")) and "token" not in n


def _looks_like_password_field(name: str) -> bool:
    n = name.lower()
    return n in {"password", "passwd", "pass", "pwd"} or "password" in n


def _looks_like_account_id_field(name: str) -> bool:
    n = name.lower()
    return n in {"account_id", "accountid", "account", "sso_account_id"} or n.endswith("[account_id]")


def _form_score(form: HtmlForm) -> int:
    keys = list(form.fields)
    score = 0
    if any(_looks_like_email_field(k) for k in keys):
        score += 10
    if any(_looks_like_password_field(k) for k in keys):
        score += 10
    if any(_looks_like_account_id_field(k) for k in keys):
        score += 12
    action = form.action.lower()
    if any(marker in action for marker in ("login", "signin", "authorize", "consent", "sso")):
        score += 3
    if form.submit_name:
        score += 1
    return score


def populate_account_form(form: HtmlForm, account: GeneratedAccount, *, user_token: str = "") -> dict[str, str]:
    data = dict(form.fields)
    has_email_field = any(_looks_like_email_field(k) for k in data)
    has_password_field = any(_looks_like_password_field(k) for k in data)
    if has_password_field and not account.password:
        raise OAuthFlowError("页面要求密码，但 IDP 未返回明文密码", stage="account_password_missing")
    for key in list(data):
        if _looks_like_email_field(key):
            data[key] = account.email
        if _looks_like_password_field(key) and account.password:
            data[key] = account.password
        if _looks_like_account_id_field(key) and account.id:
            data[key] = str(account.id)
        if key.lower() == "token" and user_token:
            data[key] = user_token
    # Some simple pages use unnamed/odd fields; add common names when absent.
    if not has_email_field:
        data.setdefault("email", account.email)
    if account.password and not has_password_field:
        data.setdefault("password", account.password)
    if user_token:
        data.setdefault("token", user_token)
    if form.submit_name:
        data.setdefault(form.submit_name, form.submit_value)
    return data


def _absolute_url(current_url: str, candidate: str) -> str:
    return urllib.parse.urljoin(current_url, html.unescape(candidate or ""))


def _has_callback_state(url: str, expected_state: str) -> bool:
    if not expected_state:
        return False
    parsed = urllib.parse.urlparse(str(url or ""))
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    return bool(query.get("code")) and str((query.get("state") or [""])[0] or "") == expected_state


class SSOHttpFlow:
    def __init__(
        self,
        *,
        timeout: float = 30,
        proxy: str = "",
        artifact_dir: str | Path | None = None,
        logger: JsonlLogger | None = None,
        session: Any | None = None,
        user_token: str = "",
    ):
        self.timeout = timeout
        self.proxy = str(proxy or "").strip()
        self.artifact_dir = Path(artifact_dir or "artifacts/idp_codex")
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger
        self.session = session or self._new_session()
        self.user_token = str(user_token or "").strip()

    def _new_session(self) -> Any:
        if CurlSession is None:  # pragma: no cover
            raise OAuthFlowError("缺少 curl_cffi 依赖，请安装项目依赖", stage="http_session")
        session = CurlSession()
        session.timeout = self.timeout
        session.headers.update({
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9,zh-CN;q=0.8",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Connection": "close",
        })
        if self.proxy:
            session.proxies = {"http": self.proxy, "https": self.proxy}
        return session

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
        json_body: Any = None,
        allow_redirects: bool = False,
    ) -> HttpResult:
        req_headers = headers or {}
        if self.logger:
            self.logger.write("oauth_http_request", {"method": method, "url": url, "headers": req_headers, "data": data, "json": json_body})
        last_exc: Exception | None = None
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                kwargs = {
                    "headers": req_headers,
                    "allow_redirects": allow_redirects,
                    "timeout": self.timeout,
                }
                if data is not None:
                    kwargs["data"] = data
                if json_body is not None:
                    kwargs["json"] = json_body
                if CurlSession is not None and hasattr(self.session, "request"):
                    kwargs.setdefault("impersonate", "chrome110")
                resp = self.session.request(method, url, **kwargs)
                break
            except TypeError:
                # Test fakes generally do not accept curl_cffi-only kwargs.
                kwargs = {"headers": req_headers, "allow_redirects": allow_redirects, "timeout": self.timeout}
                if data is not None:
                    kwargs["data"] = data
                if json_body is not None:
                    kwargs["json"] = json_body
                resp = self.session.request(method, url, **kwargs)
                break
            except Exception as exc:
                last_exc = exc
                retryable = self._is_transient_http_error(exc)
                if self.logger:
                    self.logger.write("oauth_http_error", {"method": method, "url": url, "attempt": attempt, "retryable": retryable, "error": str(exc)})
                if not retryable or attempt >= attempts:
                    raise OAuthFlowError(f"OAuth HTTP 请求失败：{exc}", stage="oauth_http", retryable=True) from exc
                time.sleep(min(3.0, 0.5 * attempt))
        else:  # pragma: no cover
            raise OAuthFlowError(f"OAuth HTTP 请求失败：{last_exc}", stage="oauth_http", retryable=True)
        text = str(getattr(resp, "text", "") or "")
        headers_out = {str(k): str(v) for k, v in dict(getattr(resp, "headers", {}) or {}).items()}
        result = HttpResult(
            status_code=int(getattr(resp, "status_code", 0) or getattr(resp, "status", 0) or 0),
            url=str(getattr(resp, "url", "") or url),
            headers=headers_out,
            text=text,
        )
        try:
            result.json_data = resp.json()
        except Exception:
            result.json_data = None
        if self.logger:
            self.logger.write("oauth_http_response", {"method": method, "url": url, "status": result.status_code, "final_url": result.url, "headers": result.headers, "body_preview": text[:500]})
        return result

    def _is_transient_http_error(self, exc: BaseException) -> bool:
        text = str(exc or "").lower()
        return any(marker in text for marker in ("curl: (52)", "empty reply", "timed out", "connection reset", "connection aborted", "ssl", "temporarily unavailable"))

    def _save_html(self, name: str, response: HttpResult) -> Path:
        path = self.artifact_dir / name
        path.write_text(response.text or "", encoding="utf-8")
        return path

    def _redirect_location(self, response: HttpResult) -> str:
        location = response.header("Location")
        if location:
            return _absolute_url(response.url, location)
        return ""

    def _extract_script_or_meta_redirect(self, response: HttpResult) -> str:
        forms, links, meta = parse_html_forms(response.text)
        if meta:
            return _absolute_url(response.url, meta)
        patterns = (
            r"location\.href\s*=\s*['\"]([^'\"]+)['\"]",
            r"window\.location\s*=\s*['\"]([^'\"]+)['\"]",
            r"window\.location\.replace\(['\"]([^'\"]+)['\"]\)",
        )
        for pattern in patterns:
            match = re.search(pattern, response.text or "", re.I)
            if match:
                return _absolute_url(response.url, match.group(1))
        for link in links:
            lowered = link.lower()
            if any(marker in lowered for marker in ("authorize", "callback", "sso", "login", "continue")):
                return _absolute_url(response.url, link)
        return ""

    def _submit_best_form(self, response: HttpResult, account: GeneratedAccount) -> HttpResult | None:
        forms, _links, _meta = parse_html_forms(response.text)
        if not forms:
            return None
        best = max(forms, key=_form_score)
        if _form_score(best) <= 0:
            return None
        action = _absolute_url(response.url, best.action or response.url)
        data = populate_account_form(best, account, user_token=self.user_token)
        method = (best.method or "GET").upper()
        if method == "GET":
            sep = "&" if urllib.parse.urlparse(action).query else "?"
            return self._request("GET", action + sep + urllib.parse.urlencode(data), headers={"Referer": response.url}, allow_redirects=False)
        return self._request("POST", action, headers={"Referer": response.url, "Content-Type": "application/x-www-form-urlencoded"}, data=data, allow_redirects=False)

    def _workspace_id_from_consent_html(self, text: str) -> str:
        patterns = (
            r'\\"workspaces\\",\[\d+\],\{.*?\},\\"id\\",\\"([^\\"]+)\\"',
            r'\\"id\\",\\"([0-9a-fA-F-]{36})\\",\\"name\\",\\"kind\\",\\"personal',
            r'"id","([0-9a-fA-F-]{36})","name","kind","personal',
            r'"workspace_id"\s*:\s*"([0-9a-fA-F-]{36})"',
        )
        for pattern in patterns:
            match = re.search(pattern, text or "")
            if match:
                return match.group(1)
        return ""

    def _try_workspace_consent(self, response: HttpResult) -> HttpResult | None:
        if "consent" not in response.url and "workspace" not in (response.text or "").lower():
            return None
        workspace_id = self._workspace_id_from_consent_html(response.text)
        if not workspace_id:
            return None
        return self._request(
            "POST",
            "https://auth.openai.com/api/accounts/workspace/select",
            headers={"accept": "application/json", "content-type": "application/json", "origin": "https://auth.openai.com", "referer": response.url},
            json_body={"workspace_id": workspace_id},
            allow_redirects=False,
        )

    def _response_continue_url(self, response: HttpResult) -> str:
        data = response.json_data if isinstance(response.json_data, dict) else {}
        for key in ("continue_url", "redirect_url", "url", "start_url"):
            value = str(data.get(key) or "").strip()
            if value:
                return _absolute_url(response.url, value)
        page = data.get("page") if isinstance(data.get("page"), dict) else {}
        payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
        value = str(payload.get("url") or "").strip()
        if value:
            return _absolute_url(response.url, value)
        return ""

    def _try_openai_identifier_continue(self, response: HttpResult, account: GeneratedAccount) -> HttpResult | None:
        parsed = urllib.parse.urlparse(response.url)
        if parsed.netloc != "auth.openai.com":
            return None
        if parsed.path not in {"/log-in", "/log-in-or-create-account"}:
            return None
        if not account.email:
            return None
        return self._request(
            "POST",
            "https://auth.openai.com/api/accounts/authorize/continue",
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": "https://auth.openai.com",
                "referer": "https://auth.openai.com/log-in-or-create-account",
            },
            json_body={"username": {"value": account.email, "kind": "email"}, "screen_hint": "login_or_signup"},
            allow_redirects=False,
        )

    def _chatgpt_login_url_from_idp_start(self, start_url: str) -> str:
        response = self._request("GET", start_url, allow_redirects=False)
        location = self._redirect_location(response)
        if location:
            return location
        scripted = self._extract_script_or_meta_redirect(response)
        if scripted:
            return scripted
        raise OAuthFlowError("IDP start-sso 未跳转到 ChatGPT SSO 登录页", stage="prime", data={"url": response.url, "status": response.status_code})

    def _chatgpt_provider_init_url(self, login_url: str) -> str:
        parsed = urllib.parse.urlparse(login_url)
        query = urllib.parse.parse_qs(parsed.query)
        connection = str((query.get("connection") or [""])[0] or "").strip()
        if not connection:
            raise OAuthFlowError("ChatGPT SSO 登录 URL 缺少 connection 参数", stage="chatgpt_signin", data={"url": login_url})

        # Load the login page first so NextAuth/ChatGPT cookies are initialized
        # in the same session, then reproduce the SPA signIn("openai", ...)
        # request observed in the bundled client script.
        self._request("GET", login_url, allow_redirects=False)
        csrf_response = self._request(
            "GET",
            "https://chatgpt.com/api/auth/csrf",
            headers={"accept": "application/json", "referer": login_url},
            allow_redirects=False,
        )
        csrf_data = csrf_response.json_data if isinstance(csrf_response.json_data, dict) else {}
        csrf_token = str(csrf_data.get("csrfToken") or "").strip()
        if not csrf_token:
            raise OAuthFlowError("ChatGPT NextAuth 未返回 csrfToken", stage="chatgpt_signin", data={"status": csrf_response.status_code})

        provider = "2" if connection.startswith("conn_") else "1"
        signin_url = "https://chatgpt.com/api/auth/signin/openai?" + urllib.parse.urlencode(
            {"connection": connection, "connection_provider": provider}
        )
        response = self._request(
            "POST",
            signin_url,
            headers={
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
                "origin": "https://chatgpt.com",
                "referer": login_url,
            },
            data={"csrfToken": csrf_token, "callbackUrl": "/", "json": "true"},
            allow_redirects=False,
        )
        data = response.json_data if isinstance(response.json_data, dict) else {}
        url = str(data.get("url") or "").strip()
        if not url:
            raise OAuthFlowError(
                "ChatGPT NextAuth signin 未返回授权 URL",
                stage="chatgpt_signin",
                data={"status": response.status_code, "response": redact(data), "body": response.text[:500]},
            )
        return _absolute_url(response.url, url)

    def _drive_until_callback(self, start_url: str, account: GeneratedAccount, *, expected_state: str, stage: str) -> str:
        current = start_url
        response: HttpResult | None = None
        for step in range(1, 31):
            if _has_callback_state(current, expected_state):
                return current
            response = self._request("GET", current, allow_redirects=False) if response is None else response
            if _has_callback_state(response.url, expected_state):
                return response.url
            location = self._redirect_location(response)
            if location:
                current = location
                response = None
                continue
            json_continue = self._response_continue_url(response)
            if json_continue:
                current = json_continue
                response = None
                continue
            scripted = self._extract_script_or_meta_redirect(response)
            if scripted:
                current = scripted
                response = None
                continue
            consent = self._try_workspace_consent(response)
            if consent is not None:
                location = self._redirect_location(consent) or self._response_continue_url(consent)
                if location:
                    current = location
                    response = None
                    continue
                response = consent
                continue
            openai_continue = self._try_openai_identifier_continue(response, account)
            if openai_continue is not None:
                response = openai_continue
                continue
            submitted = self._submit_best_form(response, account)
            if submitted is not None:
                response = submitted
                continue
            # If a page is already an authenticated terminal page, let caller proceed.
            if stage == "prime" and response.status_code < 400:
                return response.url
            path = self._save_html(f"unhandled_{stage}_{step}.html", response)
            raise OAuthFlowError(
                f"纯 HTTP 流程遇到无法自动处理的页面：{response.url}，已保存 {path}",
                stage=stage,
                data={"url": response.url, "artifact": str(path)},
            )
        if response is not None:
            path = self._save_html(f"max_steps_{stage}.html", response)
            raise OAuthFlowError(f"OAuth 流程超过最大步数，已保存 {path}", stage=stage, retryable=True, data={"artifact": str(path)})
        raise OAuthFlowError("OAuth 流程超过最大步数", stage=stage, retryable=True)

    def prime_sso_session(self, start_url: str, account: GeneratedAccount) -> str:
        try:
            login_url = self._chatgpt_login_url_from_idp_start(start_url)
        except OAuthFlowError:
            # Keep the generic form/redirect driver available for tests and
            # legacy IDP deployments that do not use ChatGPT's NextAuth SSO
            # entrypoint.
            return self._drive_until_callback(start_url, account, expected_state="", stage="prime")
        if urllib.parse.urlparse(login_url).netloc.endswith("chatgpt.com"):
            provider_url = self._chatgpt_provider_init_url(login_url)
            return self._drive_until_callback(provider_url, account, expected_state="", stage="prime")
        return self._drive_until_callback(start_url, account, expected_state="", stage="prime")

    def exchange_code(self, code: str, oauth: OAuthStart) -> dict[str, Any]:
        response = self._request(
            "POST",
            TOKEN_URL,
            headers={"content-type": "application/x-www-form-urlencoded", "accept": "application/json"},
            data={
                "grant_type": "authorization_code",
                "client_id": oauth.client_id,
                "code": code,
                "redirect_uri": oauth.redirect_uri,
                "code_verifier": oauth.code_verifier,
            },
            allow_redirects=False,
        )
        data = response.json_data if isinstance(response.json_data, dict) else {}
        if response.status_code != 200:
            raise OAuthFlowError(f"token exchange failed: HTTP {response.status_code}", stage="token_exchange", data={"response": redact(data), "raw": response.text[:500]})
        return data

    def authorize_codex(self, oauth: OAuthStart, account: GeneratedAccount) -> dict[str, Any]:
        callback_url = self._drive_until_callback(oauth.auth_url, account, expected_state=oauth.state, stage="codex_authorize")
        callback = parse_callback_url(callback_url, expected_state=oauth.state)
        if not callback.get("code"):
            raise OAuthFlowError("OAuth callback 缺少 code", stage="oauth_callback", data={"callback_url": callback_url})
        token_resp = self.exchange_code(callback["code"], oauth)
        token_config = token_config_from_response(token_resp, client_id=oauth.client_id)
        if not token_config.get("email"):
            token_config["email"] = account.email
        return token_config

    def run(self, *, start_url: str, oauth: OAuthStart, account: GeneratedAccount) -> dict[str, Any]:
        self.prime_sso_session(start_url, account)
        token_config = self.authorize_codex(oauth, account)
        if not str(token_config.get("refresh_token") or "").strip():
            raise OAuthFlowError("OAuth token 响应缺少 refresh_token", stage="token_exchange", data={"token": redact(token_config)})
        (self.artifact_dir / "token.private.json").write_text(json.dumps(redact(token_config), ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
        return token_config
