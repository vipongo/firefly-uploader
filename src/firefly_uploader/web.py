"""Web app: log in, pick a Firefly connection, upload a statement, review it, send it."""

import hmac
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData

from . import review
from .firefly import Account, FireflyClient, FireflyError
from .models import Statement
from .parsers import parse
from .rates import DailyRates, RatesError, fetch_rates
from .store import SESSION_DAYS, Connection, Rule, Session, Store, counterparty_key

HERE = Path(__file__).parent
KEEP_UPLOADS = 20
SESSION_COOKIE = "uploader_session"
MIN_PASSWORD_LENGTH = 8
CHOOSE_ACCOUNT = "Choose the Firefly account this statement belongs to."


def money(amount: Decimal) -> str:
    """Swiss style: -1'918.85"""
    return f"{amount:,.2f}".replace(",", "'")


templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["money"] = money
templates.env.filters["merchant_key"] = counterparty_key
templates.env.filters["rate"] = lambda rate: f"{rate:.6g}"  # 0.9424, 0.00258891


class PageError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class LoginRequired(Exception):
    pass


@dataclass
class FireflyStatus:
    """What Firefly says about a connection; shown at the top of its pages."""

    url: str
    user: str = ""
    version: str = ""
    error: str = ""

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc

    @property
    def key(self) -> str:
        """Identifies the Firefly user for things that only make sense for that user."""
        return f"{self.user} @ {self.url}"


@dataclass
class Upload:
    user_id: int
    connection_id: int
    filename: str
    statement: Statement


def status_of(firefly: FireflyClient) -> FireflyStatus:
    return FireflyStatus(firefly.url, user=firefly.user_email(), version=firefly.version())


def normalize_url(text: str) -> str:
    """'firefly.example.com/' or 'https://firefly.example.com/api/v1' -> 'https://firefly.example.com'"""
    url = text.strip().rstrip("/").removesuffix("/api/v1").rstrip("/")
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"“{text}” isn't a web address")
    return url


def local_path(target: str | None) -> str | None:
    """Only addresses within this app, so a link can't send someone elsewhere after logging in."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


def password_problem(password: str, again: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"The password needs at least {MIN_PASSWORD_LENGTH} characters."
    if password != again:
        return "The two passwords aren't the same."
    return ""


def apply_choices(rows: list[review.Row], form: FormData, own_accounts: list[Account]) -> None:
    included = set(form.getlist("include"))
    for number, row in enumerate(rows):
        row.include = str(number) in included
        row.counterparty = " ".join(str(form.get(f"name_{number}", "")).split()) or row.tx.counterparty
        row.choose(str(form.get(f"category_{number}", "")), own_accounts)
        remember = form.get(f"remember_{number}")
        row.remember = remember if remember in (review.REMEMBER, review.ONCE, review.ALWAYS_ASK) else review.ONCE


def create_app(
    store: Store,
    *,
    connect: Callable[[str, str], FireflyClient] = FireflyClient,
    rates: Callable[[str, str, date, date], DailyRates] = fetch_rates,
) -> FastAPI:
    """`connect(url, token)` opens a Firefly client; one is used per request. `rates` gets exchange rates."""
    app = FastAPI(title="Firefly uploader", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    uploads: dict[str, Upload] = {}  # in memory: gone when the app restarts

    def render(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def logged_in(request: Request) -> Session | None:
        secret = request.cookies.get(SESSION_COOKIE)
        return store.session(secret) if secret else None

    def current_session(request: Request) -> Session:
        """For pages that need a logged-in user."""
        session = logged_in(request)
        if session is None:
            raise LoginRequired()
        return session

    def check_form(session: Session, csrf: str | None) -> None:
        """Forms carry the session's own token, so another site can't submit them for the user."""
        if not hmac.compare_digest(str(csrf or ""), session.csrf):
            raise PageError("This form is out of date. Go back, reload the page and try again.", 403)

    def log_in(request: Request, user_id: int, target: str) -> RedirectResponse:
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            SESSION_COOKIE, store.start_session(user_id), max_age=SESSION_DAYS * 24 * 3600,
            httponly=True, samesite="lax", secure=request.url.scheme == "https",
        )
        return response

    def connection_for(session: Session, connection_id: int) -> Connection:
        connection = store.connection(session.user_id, connection_id)
        if connection is None:
            raise PageError("There's no such Firefly connection.", 404)
        if connection.token is None:
            raise PageError(
                "The token of this connection can't be read any more: the app's secret key changed. "
                "Remove the connection on the home page and add it again.", 409,
            )
        return connection

    def open_firefly(connection: Connection) -> FireflyClient:
        return connect(connection.url, connection.token)

    def check_status(connection: Connection) -> FireflyStatus:
        with open_firefly(connection) as firefly:
            try:
                return status_of(firefly)
            except FireflyError as error:
                return FireflyStatus(connection.url, user=connection.firefly_user, error=str(error))

    def upload_for(session: Session, connection: Connection, upload_id: str) -> Upload:
        current = uploads.get(upload_id)
        if current is None or (current.user_id, current.connection_id) != (session.user_id, connection.id):
            raise PageError("This upload is gone (the app was restarted?). Please upload the file again.", 404)
        return current

    def rows_for(
        firefly: FireflyClient, statement: Statement, account: Account | None,
        own_accounts: list[Account], rules: dict[str, Rule],
    ) -> list[review.Row]:
        """Rows to review, converted into the account's currency. Raises RatesError."""
        if account is None:
            return review.prepare(statement, [], rules, own_accounts=own_accounts)
        start, end = review.search_range(statement)
        daily = None
        if account.currency != statement.currency:
            daily = rates(statement.currency, account.currency, start, end)
        booked = firefly.booked(account.id, start, end)
        return review.prepare(statement, booked, rules, rates=daily, own_accounts=own_accounts)

    @app.exception_handler(LoginRequired)
    def login_required(request: Request, error: LoginRequired) -> RedirectResponse:
        if not store.has_users():
            return RedirectResponse("/setup", status_code=303)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        if request.method == "GET" and target != "/":
            return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(PageError)
    def page_error(request: Request, error: PageError) -> HTMLResponse:
        return render(request, "error.html", error.status_code, session=logged_in(request), message=str(error))

    @app.exception_handler(FireflyError)
    def firefly_error(request: Request, error: FireflyError) -> HTMLResponse:
        return render(request, "error.html", 502, session=logged_in(request), message=str(error))

    @app.get("/healthz", response_class=PlainTextResponse)
    def health() -> str:
        return "ok"

    # First start, logging in and out

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request):
        if store.has_users():
            return RedirectResponse("/login", status_code=303)
        return render(request, "setup.html")

    @app.post("/setup")
    def setup(
        request: Request, username: str = Form(""), password: str = Form(""), password_again: str = Form(""),
    ):
        if store.has_users():
            return RedirectResponse("/login", status_code=303)
        username = username.strip()
        problem = "Choose a username." if not username else password_problem(password, password_again)
        if problem:
            return render(request, "setup.html", 400, error=problem, username=username)
        return log_in(request, store.create_user(username, password), "/")

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str | None = None):
        if not store.has_users():
            return RedirectResponse("/setup", status_code=303)
        return render(request, "login.html", next=local_path(next) or "")

    @app.post("/login")
    def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("")):
        user_id = store.check_login(username.strip(), password)
        if user_id is None:
            return render(
                request, "login.html", 401,
                error="Wrong username or password.", username=username, next=local_path(next) or "",
            )
        default = store.default_connection(user_id)
        return log_in(request, user_id, local_path(next) or (f"/c/{default}" if default else "/"))

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form(""), session: Session = Depends(current_session)):
        check_form(session, csrf)
        store.end_session(request.cookies[SESSION_COOKIE])
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    # Firefly connections

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, session: Session = Depends(current_session)):
        return render(request, "home.html", session=session, connections=store.connections(session.user_id))

    @app.post("/connections")
    def add_connection(
        request: Request,
        csrf: str = Form(""),
        url: str = Form(""),
        token: str = Form(""),
        make_default: bool = Form(False),
        session: Session = Depends(current_session),
    ):
        check_form(session, csrf)
        token = token.strip()
        try:
            firefly_url = normalize_url(url)
            with connect(firefly_url, token) as firefly:
                status = status_of(firefly)
        except (ValueError, FireflyError) as error:
            return render(
                request, "home.html", 400,
                session=session, connections=store.connections(session.user_id), error=str(error), url=url,
            )
        connection_id = store.save_connection(session.user_id, firefly_url, status.user, token)
        if make_default:
            store.set_default(session.user_id, connection_id)
        return RedirectResponse(f"/c/{connection_id}", status_code=303)

    @app.post("/connections/{connection_id}/default")
    def set_default(
        connection_id: int, csrf: str = Form(""), is_default: bool = Form(False),
        session: Session = Depends(current_session),
    ):
        check_form(session, csrf)
        store.set_default(session.user_id, connection_id, is_default)
        return RedirectResponse("/", status_code=303)

    @app.post("/connections/{connection_id}/remove")
    def remove_connection(connection_id: int, csrf: str = Form(""), session: Session = Depends(current_session)):
        check_form(session, csrf)
        store.remove_connection(session.user_id, connection_id)
        return RedirectResponse("/", status_code=303)

    # Uploading and reviewing a statement for one connection

    @app.get("/c/{connection_id}", response_class=HTMLResponse)
    def upload_page(request: Request, connection_id: int, session: Session = Depends(current_session)):
        connection = connection_for(session, connection_id)
        return render(request, "upload.html", session=session, connection=connection, firefly=check_status(connection))

    @app.post("/c/{connection_id}/upload")
    async def upload(
        request: Request,
        connection_id: int,
        statement_file: UploadFile,
        csrf: str = Form(""),
        session: Session = Depends(current_session),
    ):
        check_form(session, csrf)
        connection = await run_in_threadpool(connection_for, session, connection_id)
        data = await statement_file.read()
        try:
            statement = parse(data)
            if not statement.transactions:
                raise ValueError("there are no transactions in it")
        except (ValueError, KeyError, ArithmeticError) as error:
            firefly = await run_in_threadpool(check_status, connection)
            return render(
                request, "upload.html", 400, session=session, connection=connection, firefly=firefly,
                error=f"Couldn't read {statement_file.filename}: {error}",
            )
        upload_id = uuid.uuid4().hex
        uploads[upload_id] = Upload(session.user_id, connection.id, statement_file.filename or "statement", statement)
        while len(uploads) > KEEP_UPLOADS:
            del uploads[next(iter(uploads))]
        return RedirectResponse(f"/c/{connection.id}/review/{upload_id}", status_code=303)

    @app.get("/c/{connection_id}/review/{upload_id}", response_class=HTMLResponse)
    def show_review(
        request: Request, connection_id: int, upload_id: str, account: str | None = None,
        session: Session = Depends(current_session),
    ):
        connection = connection_for(session, connection_id)
        current = upload_for(session, connection, upload_id)
        statement = current.statement
        rules = store.rules(session.user_id)
        with open_firefly(connection) as firefly:
            status = status_of(firefly)
            accounts = [a for a in firefly.asset_accounts() if a.active]
            linked = account or store.linked_account(status.key, statement.account)
            chosen = review.pick_account(statement, accounts, linked)
            own_accounts = [a for a in accounts if a != chosen]
            problem = "" if chosen else CHOOSE_ACCOUNT
            try:
                rows = rows_for(firefly, statement, chosen, own_accounts, rules)
            except RatesError as error:
                problem, rows = str(error), review.prepare(statement, [], rules, own_accounts=own_accounts)
            categories = firefly.categories()
            names = firefly.counterparty_names()
        # for the page's script, when a name is changed
        rule_choices = {
            key: {"choice": review.choice_for(rule, own_accounts), "always_ask": rule.always_ask}
            for key, rule in rules.items()
        }
        return render(
            request, "review.html",
            session=session, connection=connection, firefly=status, upload_id=upload_id, upload=current,
            statement=statement, accounts=accounts, account=chosen, own_accounts=own_accounts, problem=problem,
            categories=categories, names=names, rows=rows, rules=rule_choices,
        )

    @app.post("/c/{connection_id}/review/{upload_id}", response_class=HTMLResponse)
    async def send_review(
        request: Request, connection_id: int, upload_id: str, session: Session = Depends(current_session),
    ):
        form = await request.form()
        check_form(session, form.get("csrf"))
        return await run_in_threadpool(send_rows, request, session, connection_id, upload_id, form)

    def send_rows(
        request: Request, session: Session, connection_id: int, upload_id: str, form: FormData,
    ) -> HTMLResponse:
        connection = connection_for(session, connection_id)
        current = upload_for(session, connection, upload_id)
        statement = current.statement
        with open_firefly(connection) as firefly:
            status = status_of(firefly)
            accounts = [a for a in firefly.asset_accounts() if a.active]
            account = next((a for a in accounts if a.id == form.get("account")), None)
            if account is None:
                raise PageError(CHOOSE_ACCOUNT)
            own_accounts = [a for a in accounts if a != account]
            try:
                rows = rows_for(firefly, statement, account, own_accounts, store.rules(session.user_id))
            except RatesError as error:
                raise PageError(str(error), 502) from error
            apply_choices(rows, form, own_accounts)
            store.link_account(status.key, statement.account, account.id)
            review.save_rules(rows, store, session.user_id)
            outcomes = review.send(rows, account.id, firefly)
        counts = {
            kind: sum(o.status == kind for o in outcomes)
            for kind in (review.CREATED, review.DUPLICATE, review.FAILED, review.SKIPPED)
        }
        return render(
            request, "result.html",
            session=session, connection=connection, firefly=status, upload_id=upload_id, upload=current,
            account=account, outcomes=outcomes, counts=counts,
        )

    return app
