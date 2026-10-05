"""Web app: upload a statement, review it, send it to Firefly."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData

from . import review
from .firefly import Account, FireflyClient, FireflyError
from .models import Statement
from .parsers import parse
from .store import Store, counterparty_key

HERE = Path(__file__).parent
KEEP_UPLOADS = 20


def money(amount: Decimal) -> str:
    """Swiss style: -1'918.85"""
    return f"{amount:,.2f}".replace(",", "'")


templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["money"] = money
templates.env.filters["merchant_key"] = counterparty_key


class PageError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class Connection:
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
    filename: str
    statement: Statement


def connection_of(firefly: FireflyClient) -> Connection:
    return Connection(firefly.url, user=firefly.user_email(), version=firefly.version())


def problem_with(statement: Statement, account: Account | None) -> str:
    """Why this statement can't be sent to this account yet, if it can't."""
    if account is None:
        return "Choose the Firefly account this statement belongs to."
    if account.currency != statement.currency:
        return (
            f"This statement is in {statement.currency} but {account.name} is in {account.currency}. "
            "Converting currencies isn't built yet."
        )
    return ""


def apply_choices(rows: list[review.Row], form: FormData) -> None:
    included = set(form.getlist("include"))
    for number, row in enumerate(rows):
        row.include = str(number) in included
        row.counterparty = " ".join(str(form.get(f"name_{number}", "")).split()) or row.tx.counterparty
        row.category = str(form.get(f"category_{number}", ""))
        remember = form.get(f"remember_{number}")
        row.remember = remember if remember in (review.REMEMBER, review.ONCE, review.ALWAYS_ASK) else review.ONCE


def create_app(connect: Callable[[], FireflyClient], store: Store) -> FastAPI:
    """`connect` opens a Firefly client; one is used per request."""
    app = FastAPI(title="Firefly uploader", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    uploads: dict[str, Upload] = {}  # in memory: gone when the app restarts

    def render(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def upload_for(upload_id: str) -> Upload:
        if upload_id not in uploads:
            raise PageError("This upload is gone (the app was restarted?). Please upload the file again.", 404)
        return uploads[upload_id]

    def current_connection() -> Connection:
        with connect() as firefly:
            try:
                return connection_of(firefly)
            except FireflyError as error:
                return Connection(firefly.url, error=str(error))

    @app.exception_handler(PageError)
    def page_error(request: Request, error: PageError) -> HTMLResponse:
        return render(request, "error.html", error.status_code, message=str(error))

    @app.exception_handler(FireflyError)
    def firefly_error(request: Request, error: FireflyError) -> HTMLResponse:
        return render(request, "error.html", 502, message=str(error))

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return render(request, "index.html", connection=current_connection())

    @app.post("/upload")
    async def upload(request: Request, statement_file: UploadFile):
        data = await statement_file.read()
        try:
            statement = parse(data)
            if not statement.transactions:
                raise ValueError("there are no transactions in it")
        except (ValueError, KeyError, ArithmeticError) as error:
            connection = await run_in_threadpool(current_connection)
            message = f"Couldn't read {statement_file.filename}: {error}"
            return render(request, "index.html", 400, connection=connection, error=message)
        upload_id = uuid.uuid4().hex
        uploads[upload_id] = Upload(statement_file.filename or "statement", statement)
        while len(uploads) > KEEP_UPLOADS:
            del uploads[next(iter(uploads))]
        return RedirectResponse(f"/review/{upload_id}", status_code=303)

    @app.get("/review/{upload_id}", response_class=HTMLResponse)
    def show_review(request: Request, upload_id: str, account: str | None = None):
        current = upload_for(upload_id)
        statement = current.statement
        with connect() as firefly:
            connection = connection_of(firefly)
            accounts = [a for a in firefly.asset_accounts() if a.active]
            linked = account or store.linked_account(connection.key, statement.account)
            chosen = review.pick_account(statement, accounts, linked)
            booked = firefly.booked(chosen.id, *review.search_range(statement)) if chosen else []
            categories = firefly.categories()
            names = firefly.counterparty_names()
        rules = store.rules()
        return render(
            request, "review.html",
            connection=connection, upload_id=upload_id, upload=current, statement=statement,
            accounts=accounts, account=chosen, problem=problem_with(statement, chosen),
            categories=categories, names=names, rows=review.prepare(statement, booked, rules),
            # for the page's script, when a name is changed
            rules={key: {"category": rule.category, "always_ask": rule.always_ask} for key, rule in rules.items()},
        )

    @app.post("/review/{upload_id}", response_class=HTMLResponse)
    async def send_review(request: Request, upload_id: str):
        current = upload_for(upload_id)
        form = await request.form()
        return await run_in_threadpool(send_rows, request, upload_id, current, form)

    def send_rows(request: Request, upload_id: str, current: Upload, form: FormData) -> HTMLResponse:
        statement = current.statement
        with connect() as firefly:
            connection = connection_of(firefly)
            account = next((a for a in firefly.asset_accounts() if a.id == form.get("account")), None)
            if problem := problem_with(statement, account):
                raise PageError(problem)
            booked = firefly.booked(account.id, *review.search_range(statement))
            rows = review.prepare(statement, booked, store.rules())
            apply_choices(rows, form)
            store.link_account(connection.key, statement.account, account.id)
            review.save_rules(rows, store)
            outcomes = review.send(rows, account.id, firefly)
        counts = {status: sum(o.status == status for o in outcomes)
                  for status in (review.CREATED, review.DUPLICATE, review.FAILED, review.SKIPPED)}
        return render(
            request, "result.html",
            connection=connection, upload_id=upload_id, upload=current, account=account,
            outcomes=outcomes, counts=counts,
        )

    return app
