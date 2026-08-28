from datetime import date, timedelta
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select

from app.main import create_app
from app.models import (
    Account,
    Campaign,
    DailyStat,
    Product,
    ProductComment,
    SchedulerSetting,
    SyncRun,
    User,
)
from app.services.wildberries import WBApiError


def register_user(
    client: TestClient,
    username: str = "owner",
    password: str = "strong-password-123",
):
    return client.post(
        "/register",
        data={
            "username": username,
            "password": password,
            "password_confirm": password,
            "next": "/",
        },
        follow_redirects=True,
    )


def test_ui_configures_account_and_scheduler(
    settings,
    fake_client_factory,
):
    app = create_app(settings, fake_client_factory)
    with TestClient(app) as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")

        response = register_user(client)
        assert response.status_code == 200
        assert "Кабинет не настроен" in response.text

        response = client.post(
            "/cabinet",
            data={
                "name": "Основной WB",
                "token": "valid-token-123456",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Настройки кабинета сохранены" in response.text
        assert "valid-token-123456" not in response.text

        response = client.post("/cabinet/test", follow_redirects=True)
        assert response.status_code == 200
        assert "Подключение работает" in response.text

        response = client.post(
            "/scheduler",
            data={
                "enabled": "on",
                "run_time": "05:37",
                "timezone_name": "Europe/Moscow",
                "lookback_days": "5",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Расписание обновлено" in response.text

        with app.state.session_factory() as session:
            account = session.scalar(select(Account))
            assert account.name == "Основной WB"
            assert "valid-token" not in account.encrypted_token
            setting = session.scalar(select(SchedulerSetting))
            assert setting.enabled is True
            assert setting.run_time.hour == 5
            assert setting.run_time.minute == 37
            assert setting.lookback_days == 5

        response = client.get("/export.xlsx")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(
            "application/vnd.openxmlformats"
        )
        assert response.content[:2] == b"PK"


def test_manual_ui_flow_persists_filters_groups_and_exports(
    settings,
    fake_client_factory,
):
    app = create_app(settings, fake_client_factory)
    target_date = date(2026, 7, 27)

    with TestClient(app) as client:
        register_user(client)
        client.post(
            "/cabinet",
            data={
                "name": "Рабочий кабинет",
                "token": "valid-token-123456",
            },
        )
        with app.state.session_factory() as session:
            account = session.scalar(select(Account))
            encrypted_before = account.encrypted_token

        response = client.post(
            "/scheduler/run",
            data={
                "date_from": target_date.isoformat(),
                "date_to": target_date.isoformat(),
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Сбор запущен в фоне" in response.text

        with app.state.session_factory() as session:
            run = session.scalar(select(SyncRun))
            assert run.status == "success"
            assert run.records_upserted == 1
            product = session.scalar(select(Product))
            product_id = product.id

        response = client.get("/", follow_redirects=False)
        assert response.status_code == 200
        assert "500 ₽" in response.text
        assert "6 000 ₽" in response.text
        assert "3 заказов" in response.text
        assert "8.33%" in response.text

        response = client.get("/data", params={"group": "Фотообои"})
        assert response.status_code == 200
        assert "1 строк" in response.text
        assert "Фотообои Горы" in response.text
        assert "699712395" in response.text
        assert "Фотообои · поиск" in response.text

        response = client.post(
            "/products/group",
            data={
                "product_id": product_id,
                "report_group": "Настенная графика",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Группа товара обновлена" in response.text

        response = client.get(
            "/data",
            params={"group": "Настенная графика"},
        )
        assert "1 строк" in response.text
        assert "Настенная графика" in response.text

        response = client.get(
            "/export.xlsx",
            params={
                "date_from": target_date.isoformat(),
                "date_to": target_date.isoformat(),
            },
        )
        assert response.status_code == 200
        workbook = load_workbook(
            BytesIO(response.content),
            data_only=False,
        )
        summary = workbook["Итого по группам"]
        assert summary["A3"].value == "Настенная графика"
        assert str(summary["C3"].value).startswith("=")
        assert str(summary["D3"].value).startswith("=SUMIFS(")
        detail = workbook["По артикулам"]
        assert detail["A3"].value == "Настенная графика"
        assert detail["C3"].value == 699712395
        assert str(detail["E3"].value).startswith("=")
        assert str(detail["F3"].value).startswith("=SUMIFS(")

        response = client.post(
            "/cabinet",
            data={
                "name": "Кабинет после переименования",
                "token": "",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        with app.state.session_factory() as session:
            account = session.scalar(select(Account))
            assert account.name == "Кабинет после переименования"
            assert account.encrypted_token == encrypted_before


def test_analytics_report_has_categories_kpis_and_filtered_rankings(
    settings,
    fake_client_factory,
):
    app = create_app(settings, fake_client_factory)
    target_date = date(2026, 7, 27)

    with TestClient(app) as client:
        register_user(client)
        client.post(
            "/cabinet",
            data={
                "name": "Аналитический кабинет",
                "token": "valid-token-123456",
            },
        )
        client.post(
            "/scheduler/run",
            data={
                "date_from": target_date.isoformat(),
                "date_to": target_date.isoformat(),
            },
        )
        with app.state.session_factory() as session:
            account = session.scalar(select(Account))
            session.add_all(
                [
                    Campaign(
                        account_id=account.id,
                        advert_id=28000002,
                        name="Футболки · поиск",
                    ),
                    Product(
                        account_id=account.id,
                        nm_id=700000002,
                        name="Футболка Север",
                        subject_name="футболки",
                        report_group="Футболки",
                    ),
                    DailyStat(
                        account_id=account.id,
                        advert_id=28000002,
                        nm_id=700000002,
                        stat_date=target_date,
                        views=200,
                        clicks=20,
                        spend=100,
                        atbs=5,
                        orders=2,
                        canceled=0,
                        shks=2,
                        revenue=1000,
                    ),
                    DailyStat(
                        account_id=account.id,
                        advert_id=28000002,
                        nm_id=700000002,
                        stat_date=date(2026, 7, 26),
                        views=100,
                        clicks=10,
                        spend=50,
                        atbs=2,
                        orders=1,
                        canceled=0,
                        shks=1,
                        revenue=500,
                    ),
                ]
            )
            session.commit()

        response = client.get(
            "/",
            params={
                "date_from": target_date.isoformat(),
                "date_to": target_date.isoformat(),
            },
        )
        assert response.status_code == 200
        assert "Полный отчёт по РК" in response.text
        assert "Показатели из Excel" in response.text
        assert "Категории WB" in response.text
        assert "Фотообои" in response.text
        assert "Футболки" in response.text
        assert "Футболки · поиск" in response.text
        assert "Конв. корзины" in response.text
        assert "700000002" in response.text
        assert 'data-funnel-label="Фотообои"' in response.text
        assert 'data-funnel-label="Футболки"' in response.text
        assert 'data-funnel-value="orders"' in response.text

        response = client.get(
            "/",
            params={
                "date_from": target_date.isoformat(),
                "date_to": target_date.isoformat(),
                "subject": "Футболки",
            },
        )
        assert response.status_code == 200
        assert "Футболка Север" in response.text
        assert "Фотообои Горы" not in response.text
        assert "1 000 ₽" in response.text
        assert "100 ₽" in response.text
        assert "↑ 100.0%" in response.text

        response = client.get(
            "/",
            params={
                "date_from": "2026-07-28",
                "date_to": "2026-07-27",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Дата начала не может быть позже даты окончания" in response.text


def test_data_pagination_and_product_comments_are_account_scoped(
    settings,
    fake_client_factory,
):
    app = create_app(settings, fake_client_factory)
    report_group = "Тест & Данные"
    start_date = date(2026, 1, 1)

    with TestClient(app) as client:
        register_user(client)
        client.post(
            "/cabinet",
            data={
                "name": "Кабинет с комментариями",
                "token": "valid-token-123456",
            },
        )
        with app.state.session_factory() as session:
            account = session.scalar(select(Account))
            session.add(
                Campaign(
                    account_id=account.id,
                    advert_id=99000001,
                    name="Тест пагинации",
                )
            )
            session.add(
                Product(
                    account_id=account.id,
                    nm_id=880000001,
                    name="Товар с заметкой",
                    subject_name="Тест",
                    report_group=report_group,
                )
            )
            session.add_all(
                [
                    DailyStat(
                        account_id=account.id,
                        advert_id=99000001,
                        nm_id=880000001,
                        stat_date=start_date + timedelta(days=index),
                        views=index + 1,
                        clicks=1,
                        spend=10,
                        atbs=1,
                        orders=1,
                        revenue=100,
                    )
                    for index in range(61)
                ]
            )
            session.commit()

        first_page = client.get("/data", params={"group": report_group})
        assert first_page.status_code == 200
        assert "Страница 1 из 2" in first_page.text
        assert "02.03.2026" in first_page.text
        assert "01.01.2026" not in first_page.text
        assert "page=2" in first_page.text
        assert "%26" in first_page.text

        second_page = client.get(
            "/data",
            params={"page": 2, "group": report_group},
        )
        assert second_page.status_code == 200
        assert "Страница 2 из 2" in second_page.text
        assert "01.01.2026" in second_page.text
        assert "02.03.2026" not in second_page.text
        assert 'aria-current="page">2' in second_page.text

        response = client.post(
            "/comments",
            data={
                "nm_id": "880000001",
                "comment": "Проверить ставку после выходных",
                "return_to": "/data?page=2",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Комментарий к артикулу 880000001 добавлен" in response.text
        assert "Проверить ставку после выходных" in response.text

        response = client.post(
            "/comments",
            data={
                "nm_id": "880000001",
                "comment": "Ставка проверена, наблюдаем неделю",
                "return_to": "/data",
            },
            follow_redirects=True,
        )
        assert "Комментарий к артикулу 880000001 обновлён" in response.text
        assert "Ставка проверена, наблюдаем неделю" in response.text
        assert "Проверить ставку после выходных" not in response.text

        response = client.post(
            "/comments",
            data={
                "nm_id": "880000999",
                "comment": "Артикул ещё не появился в статистике",
                "return_to": "/data",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Артикул 880000999" in response.text
        assert "Артикул ещё не появился в статистике" in response.text

        with app.state.session_factory() as session:
            owner_comments = session.scalars(
                select(ProductComment).order_by(ProductComment.nm_id)
            ).all()
            assert len(owner_comments) == 2
            assert owner_comments[0].comment == (
                "Ставка проверена, наблюдаем неделю"
            )

        client.post("/logout")
        register_user(client, "second-owner", "other-password-123")
        client.post(
            "/cabinet",
            data={
                "name": "Второй кабинет",
                "token": "second-valid-token-123456",
            },
        )
        response = client.get("/data")
        assert "Ставка проверена, наблюдаем неделю" not in response.text
        assert "Артикул ещё не появился в статистике" not in response.text

        client.post(
            "/comments",
            data={
                "nm_id": "880000001",
                "comment": "Комментарий второго кабинета",
                "return_to": "/data",
            },
        )
        with app.state.session_factory() as session:
            saved_comments = session.scalars(
                select(ProductComment).order_by(ProductComment.id)
            ).all()
            assert len(saved_comments) == 3
            assert saved_comments[-1].comment == (
                "Комментарий второго кабинета"
            )


def test_failed_manual_run_is_recorded_without_exposing_token(settings):
    secret_token = "super-secret-token-123456"

    class UnauthorizedWBClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def list_campaigns(self):
            raise WBApiError(
                "Wildberries API вернул HTTP 401: unauthorized",
                status_code=401,
            )

    app = create_app(settings, lambda _token: UnauthorizedWBClient())
    target_date = date(2026, 7, 27)

    with TestClient(app) as client:
        register_user(client)
        client.post(
            "/cabinet",
            data={"name": "Кабинет с ошибкой", "token": secret_token},
        )
        response = client.post(
            "/scheduler/run",
            data={
                "date_from": target_date.isoformat(),
                "date_to": target_date.isoformat(),
            },
            follow_redirects=True,
        )
        assert response.status_code == 200

        with app.state.session_factory() as session:
            run = session.scalar(select(SyncRun))
            assert run.status == "failed"
            assert run.records_received == 0
            assert run.records_upserted == 0
            assert "HTTP 401" in run.message
            assert secret_token not in run.message

        response = client.get("/scheduler")
        assert "Ошибка" in response.text
        assert "HTTP 401" in response.text
        assert secret_token not in response.text


def test_registration_login_logout_and_legacy_account_claim(
    settings,
    fake_client_factory,
):
    app = create_app(settings, fake_client_factory)
    password = "very-strong-password-123"

    with TestClient(app) as client:
        with app.state.session_factory() as session:
            legacy = Account(name="Существующий кабинет")
            session.add(legacy)
            session.flush()
            session.add(SchedulerSetting(account_id=legacy.id))
            session.commit()
            legacy_id = legacy.id

        response = register_user(client, "Owner.User", password)
        assert response.status_code == 200
        assert "Аккаунт создан" in response.text
        assert "Существующий кабинет" in response.text
        set_cookie = response.history[0].headers["set-cookie"]
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie
        assert "wb_ads_session=" in set_cookie

        with app.state.session_factory() as session:
            user = session.scalar(select(User))
            account = session.get(Account, legacy_id)
            assert user.username == "owner.user"
            assert password not in user.password_hash
            assert user.password_hash.startswith("scrypt$")
            assert account.user_id == user.id

        response = client.post("/logout", follow_redirects=True)
        assert response.status_code == 200
        assert "Вы вышли из аккаунта" in response.text

        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")

        response = client.post(
            "/login",
            data={
                "username": "OWNER.USER",
                "password": "wrong-password",
                "next": "/",
            },
            follow_redirects=True,
        )
        assert "Неверный логин или пароль" in response.text

        response = client.post(
            "/login",
            data={
                "username": "OWNER.USER",
                "password": password,
                "next": "/",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Вход выполнен" in response.text
        assert "Существующий кабинет" in response.text


def test_users_cannot_access_each_others_cabinet_data(
    settings,
    fake_client_factory,
):
    app = create_app(settings, fake_client_factory)
    target_date = date(2026, 7, 27)

    with TestClient(app) as client:
        register_user(client, "first-user")
        client.post(
            "/cabinet",
            data={
                "name": "Первый кабинет",
                "token": "first-valid-token-123",
            },
        )
        client.post(
            "/scheduler/run",
            data={
                "date_from": target_date.isoformat(),
                "date_to": target_date.isoformat(),
            },
        )
        with app.state.session_factory() as session:
            first_user = session.scalar(
                select(User).where(User.username == "first-user")
            )
            first_account = session.scalar(
                select(Account).where(Account.user_id == first_user.id)
            )
            first_product = session.scalar(
                select(Product).where(
                    Product.account_id == first_account.id
                )
            )
            first_product_id = first_product.id

        client.post("/logout")
        register_user(client, "second-user")

        response = client.get("/")
        assert "Кабинет не настроен" in response.text
        assert "500 ₽" not in response.text

        response = client.post(
            "/products/group",
            data={
                "product_id": first_product_id,
                "report_group": "Чужая группа",
            },
            follow_redirects=True,
        )
        assert "Товар не найден" in response.text

        client.post(
            "/cabinet",
            data={
                "name": "Второй кабинет",
                "token": "second-valid-token-456",
            },
        )
        client.post(
            "/scheduler/run",
            data={
                "date_from": target_date.isoformat(),
                "date_to": target_date.isoformat(),
            },
        )

        with app.state.session_factory() as session:
            users = session.scalars(select(User).order_by(User.id)).all()
            accounts = session.scalars(select(Account).order_by(Account.id)).all()
            assert len(users) == 2
            assert len(accounts) == 2
            assert accounts[0].user_id == users[0].id
            assert accounts[1].user_id == users[1].id
            assert session.scalar(select(Product).where(Product.id == first_product_id)).report_group == "Фотообои"
            stats = session.scalars(
                select(DailyStat).order_by(DailyStat.account_id)
            ).all()
            assert len(stats) == 2
            assert {stat.account_id for stat in stats} == {
                accounts[0].id,
                accounts[1].id,
            }
