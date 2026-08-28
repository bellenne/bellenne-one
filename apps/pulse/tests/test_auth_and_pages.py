from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import delete, select

from app.models import (
    DailyMetric,
    MarketplaceAccount,
    MarketplaceProduct,
    MonthlyPlan,
    PlanAllocation,
    PlanValue,
    ProductGroup,
    User,
)
from app.security import decrypt_credentials
from app.services.planning import load_plan, plan_monthly_values
from tests.conftest import csrf_from


def test_database_has_no_seeded_base_user(client):
    with client.app.state.session_factory() as session:
        assert session.scalar(select(User).where(User.username == "admin")) is None


def test_registration_and_login_rejects_invalid_password(client):
    register_page = client.get("/register")
    registered = client.post(
        "/register",
        data={
            "csrf_token": csrf_from(register_page.text),
            "username": "login-check",
            "password": "strong-password",
            "password_confirm": "strong-password",
        },
        follow_redirects=False,
    )
    assert registered.status_code == 303
    client.cookies.clear()
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"csrf_token": csrf_from(page.text), "username": "login-check", "password": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_private_pages_render(authenticated_client):
    for path, marker in (
        ("/", "Факт, план и динамика"),
        ("/accounts", "Подключённые кабинеты"),
        ("/plans", "Обновление плана"),
        ("/reports", "Переработка тест"),
    ):
        response = authenticated_client.get(path)
        assert response.status_code == 200
        assert marker in response.text


def test_dashboard_contains_full_metric_set(authenticated_client):
    response = authenticated_client.get("/")
    for label in (
        "Заказано (шт)",
        "Сумма заказов (руб)",
        "Средний чек заказа",
        "Выкуплено (шт)",
        "Сумма выкупов (руб)",
        "РК Денег",
        "РК Бонусов",
        "Общий ДРРв (%)",
        "Общий ДРРз (%)",
        "Денег ДРРв (%)",
        "Денег ДРРз (%)",
    ):
        assert label in response.text
    assert response.text.count("metric-chart-card") == 11


def test_post_requires_csrf(authenticated_client):
    response = authenticated_client.post("/product-groups", data={"name": "Без CSRF"})
    assert response.status_code == 403


def test_create_account_encrypts_credentials(authenticated_client):
    page = authenticated_client.get("/accounts")
    response = authenticated_client.post(
        "/accounts",
        data={
            "csrf_token": csrf_from(page.text),
            "name": "Тестовый WB",
            "marketplace": "WB",
            "sync_time": "07:30",
            "timezone": "Europe/Moscow",
            "wb_api_token": "plain-test-token",
            "wb_advert_token": "",
            "schedule_enabled": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with authenticated_client.app.state.session_factory() as session:
        account = session.scalar(select(MarketplaceAccount).where(MarketplaceAccount.name == "Тестовый WB"))
        assert account is not None
        assert "plain-test-token" not in account.encrypted_credentials
        assert decrypt_credentials(
            authenticated_client.app.state.credential_cipher,
            account.encrypted_credentials,
        )["api_token"] == "plain-test-token"


def test_demo_account_can_be_deleted(authenticated_client):
    with authenticated_client.app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.username == "test-owner"))
        account = MarketplaceAccount(
            user_id=user.id,
            name="Удаляемый демо-кабинет",
            marketplace="WB",
            is_demo=True,
            schedule_enabled=False,
        )
        session.add(account)
        session.commit()
        account_id = account.id
    page = authenticated_client.get("/accounts")
    response = authenticated_client.post(
        f"/accounts/{account_id}/delete",
        data={"csrf_token": csrf_from(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with authenticated_client.app.state.session_factory() as session:
        assert session.get(MarketplaceAccount, account_id) is None


def test_product_can_be_disabled_per_account(authenticated_client):
    with authenticated_client.app.state.session_factory() as session:
        product = session.scalar(
            select(MarketplaceProduct)
            .join(MarketplaceAccount)
            .where(MarketplaceAccount.user.has(username="test-owner"))
        )
        assert product is not None
        product_id = product.id
        initial_state = product.is_active
    page = authenticated_client.get("/accounts")
    response = authenticated_client.post(
        f"/products/{product_id}/toggle",
        data={"csrf_token": csrf_from(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with authenticated_client.app.state.session_factory() as session:
        assert session.get(MarketplaceProduct, product_id).is_active is (not initial_state)
        session.get(MarketplaceProduct, product_id).is_active = initial_state
        session.commit()


def test_total_plan_post_allocates_and_future_month_isolated(authenticated_client):
    page = authenticated_client.get("/plans?period=2027-12&marketplace=WB")
    with authenticated_client.app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.username == "test-owner"))
        groups = list(session.scalars(select(ProductGroup).where(ProductGroup.user_id == user.id).order_by(ProductGroup.id)))
    payload = {
        "csrf_token": csrf_from(page.text),
        "period": "2027-12",
        "marketplace": "WB",
        "mode": "total",
        "total_ordered_units": "310",
        "total_ordered_amount": "310000",
        "total_buyout_units": "155",
        "total_buyout_amount": "155000",
        "total_net_revenue": "120000",
        "total_ad_spend": "31000",
        "total_ad_bonus_spend": "3100",
        "total_gross_profit": "12345.6789",
    }
    for index, group in enumerate(groups):
        payload[f"allocation_{group.id}"] = "100" if index == 0 else "0"
    response = authenticated_client.post("/plans", data=payload, follow_redirects=False)
    assert response.status_code == 303

    with authenticated_client.app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.username == "test-owner"))
        plan = load_plan(session, user.id, date(2027, 12, 1), "WB")
        current_plan = load_plan(session, user.id, date.today(), "WB")
        assert plan is not None
        assert plan_monthly_values(plan)["ordered_amount"] == 310000
        assert plan_monthly_values(plan)["gross_profit"] == Decimal("12345.68")
        assert plan_monthly_values(plan, groups[0].id)["ordered_amount"] == 310000
        assert current_plan.period != plan.period
    rendered = authenticated_client.get("/plans?period=2027-12&marketplace=WB")
    assert 'name="total_gross_profit" value="12345.68"' in rendered.text


def test_by_product_plan_aggregates_to_total(authenticated_client):
    page = authenticated_client.get("/plans?period=2027-11&marketplace=OZON")
    with authenticated_client.app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.username == "test-owner"))
        groups = list(session.scalars(select(ProductGroup).where(ProductGroup.user_id == user.id).order_by(ProductGroup.id)))
    payload = {
        "csrf_token": csrf_from(page.text),
        "period": "2027-11",
        "marketplace": "OZON",
        "mode": "by_product",
    }
    for index, group in enumerate(groups, start=1):
        for field in (
            "ordered_units",
            "ordered_amount",
            "buyout_units",
            "buyout_amount",
            "net_revenue",
            "ad_spend",
            "ad_bonus_spend",
            "gross_profit",
        ):
            payload[f"group_{group.id}_{field}"] = str(index * 10)
    response = authenticated_client.post("/plans", data=payload, follow_redirects=False)
    assert response.status_code == 303

    with authenticated_client.app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.username == "test-owner"))
        plan = load_plan(session, user.id, date(2027, 11, 1), "OZON")
        assert plan is not None
        expected = sum(index * 10 for index in range(1, len(groups) + 1))
        assert plan_monthly_values(plan)["ordered_amount"] == expected


def test_categories_merge_products_history_allocations_and_group_plans(authenticated_client):
    metric_date = date(2036, 1, 10)
    plan_periods = (date(2036, 1, 1), date(2036, 2, 1))
    with authenticated_client.app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.username == "test-owner"))
        account = session.scalar(
            select(MarketplaceAccount).where(
                MarketplaceAccount.user_id == user.id,
                MarketplaceAccount.is_demo.is_(True),
            )
        )
        target = ProductGroup(user_id=user.id, name="Merge Target", code="merge-target")
        source_a = ProductGroup(user_id=user.id, name="Merge Source A", code="merge-source-a")
        source_b = ProductGroup(user_id=user.id, name="Merge Source B", code="merge-source-b", is_active=False)
        session.add_all([target, source_a, source_b])
        session.flush()

        products = [
            MarketplaceProduct(
                account_id=account.id,
                product_group_id=source_a.id,
                external_id="merge-product-a",
                name="Merge Product A",
            ),
            MarketplaceProduct(
                account_id=account.id,
                product_group_id=source_b.id,
                external_id="merge-product-b",
                name="Merge Product B",
            ),
        ]
        session.add_all(products)
        for group, units, amount in (
            (target, 1, Decimal("100")),
            (source_a, 2, Decimal("200")),
            (source_b, 3, Decimal("300")),
        ):
            session.add(
                DailyMetric(
                    user_id=user.id,
                    account_id=account.id,
                    marketplace=account.marketplace,
                    metric_date=metric_date,
                    scope_key=f"group:{group.id}",
                    product_group_id=group.id,
                    ordered_units=units,
                    ordered_amount=amount,
                    ad_spend=amount / Decimal("10"),
                )
            )
        session.add(
            DailyMetric(
                user_id=user.id,
                account_id=account.id,
                marketplace=account.marketplace,
                metric_date=metric_date,
                scope_key="total",
                ordered_units=3,
                ordered_amount=Decimal("300"),
                ad_spend=Decimal("30"),
            )
        )

        allocation_plan = MonthlyPlan(
            user_id=user.id,
            period=plan_periods[0],
            marketplace="WB",
            mode="total",
        )
        group_plan = MonthlyPlan(
            user_id=user.id,
            period=plan_periods[1],
            marketplace="OZON",
            mode="by_product",
        )
        session.add_all([allocation_plan, group_plan])
        session.flush()
        session.add_all(
            [
                PlanAllocation(plan_id=allocation_plan.id, product_group_id=target.id, percentage=Decimal("10")),
                PlanAllocation(plan_id=allocation_plan.id, product_group_id=source_a.id, percentage=Decimal("20")),
                PlanAllocation(plan_id=allocation_plan.id, product_group_id=source_b.id, percentage=Decimal("30")),
                PlanValue(plan_id=group_plan.id, product_group_id=None, scope_key="total", metric_key="ordered_amount", monthly_value=Decimal("600")),
                PlanValue(plan_id=group_plan.id, product_group_id=target.id, scope_key=f"group:{target.id}", metric_key="ordered_amount", monthly_value=Decimal("100")),
                PlanValue(plan_id=group_plan.id, product_group_id=source_a.id, scope_key=f"group:{source_a.id}", metric_key="ordered_amount", monthly_value=Decimal("200")),
                PlanValue(plan_id=group_plan.id, product_group_id=source_b.id, scope_key=f"group:{source_b.id}", metric_key="ordered_amount", monthly_value=Decimal("300")),
            ]
        )
        session.commit()
        target_id = target.id
        source_ids = [source_a.id, source_b.id]
        product_ids = [product.id for product in products]
        allocation_plan_id = allocation_plan.id
        group_plan_id = group_plan.id

    page = authenticated_client.get("/accounts")
    response = authenticated_client.post(
        "/product-groups/merge",
        data={
            "csrf_token": csrf_from(page.text),
            "target_group_id": str(target_id),
            "source_group_ids": [str(group_id) for group_id in source_ids],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with authenticated_client.app.state.session_factory() as session:
        assert all(session.get(ProductGroup, group_id) is None for group_id in source_ids)
        assert all(session.get(MarketplaceProduct, product_id).product_group_id == target_id for product_id in product_ids)

        group_rows = list(
            session.scalars(
                select(DailyMetric).where(
                    DailyMetric.metric_date == metric_date,
                    DailyMetric.product_group_id == target_id,
                )
            )
        )
        assert len(group_rows) == 1
        assert group_rows[0].ordered_units == 6
        assert group_rows[0].ordered_amount == Decimal("600")
        total = session.scalar(
            select(DailyMetric).where(
                DailyMetric.metric_date == metric_date,
                DailyMetric.account_id == group_rows[0].account_id,
                DailyMetric.scope_key == "total",
            )
        )
        assert total.ordered_units == 6
        assert total.ordered_amount == Decimal("600")

        allocation = session.scalar(
            select(PlanAllocation).where(
                PlanAllocation.plan_id == allocation_plan_id,
                PlanAllocation.product_group_id == target_id,
            )
        )
        assert allocation.percentage == Decimal("60")
        assert session.scalar(
            select(PlanAllocation).where(
                PlanAllocation.plan_id == allocation_plan_id,
                PlanAllocation.product_group_id.in_(source_ids),
            )
        ) is None

        group_value = session.scalar(
            select(PlanValue).where(
                PlanValue.plan_id == group_plan_id,
                PlanValue.product_group_id == target_id,
                PlanValue.metric_key == "ordered_amount",
            )
        )
        assert group_value.monthly_value == Decimal("600")

        session.execute(delete(MarketplaceProduct).where(MarketplaceProduct.id.in_(product_ids)))
        session.execute(delete(DailyMetric).where(DailyMetric.metric_date == metric_date))
        session.execute(delete(MonthlyPlan).where(MonthlyPlan.id.in_([allocation_plan_id, group_plan_id])))
        session.delete(session.get(ProductGroup, target_id))
        session.commit()
