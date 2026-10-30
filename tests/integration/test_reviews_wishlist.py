# ruff: noqa: F811  (fixtures imported from other suites are re-declared as test parameters)
"""Reviews (only by customers who bought the product, one each, moderated) and wishlists."""

import asyncio

from sqlalchemy import text
from test_cart_variants import add, signup
from test_order_payment_lifecycle import category, hook, jobs, pay, provider  # noqa: F401
from test_variant_admin import make_product, tee
from test_variant_checkout import checkout

API = "/api/v1"


async def buy(client, headers, product_id=None, variant_id=None, *, pay_it=True, provider=None, fulfil_with=None):
    body = {"variant_id": variant_id, "quantity": 1} if variant_id else {"product_id": product_id, "quantity": 1}
    await add(client, headers, **body)
    order = (await checkout(client, headers)).json()
    if pay_it:
        await pay(client, headers, order["id"])
        assert (await hook(client, provider.last_pi)).status_code == 200
    if fulfil_with:
        assert (await client.patch(f"{API}/orders/{order['id']}/status", json={"status": "fulfilled"}, headers=fulfil_with)).status_code == 200
    return order


async def review(client, headers, product_id, **body):
    return await client.post(f"{API}/products/{product_id}/reviews", json={"rating": 5, "title": "Great", "body": "Fits well.", **body}, headers=headers)


# ============================================================================================== reviews
async def test_only_customers_who_bought_the_product_can_review_it_and_only_once(client, admin_headers, category, provider):
    p = await make_product(client, admin_headers, category, stock=10, status="active")
    stranger, _ = await signup(client)
    r = await review(client, stranger, p["id"])
    assert r.status_code == 403 and r.json()["code"] == "review_requires_purchase"
    assert (await client.post(f"{API}/products/{p['id']}/reviews", json={"rating": 5})).status_code in (401, 403)       # not signed in

    unpaid, _ = await signup(client)
    await buy(client, unpaid, p["id"], pay_it=False)                                                                  # ordered, not paid
    assert (await review(client, unpaid, p["id"])).status_code == 403

    buyer, _ = await signup(client)
    await buy(client, buyer, p["id"], provider=provider)
    created = await review(client, buyer, p["id"], rating=4)
    assert created.status_code == 201
    body = created.json()
    assert (body["rating"], body["title"], body["body"], body["verified_purchase"], body["status"]) == (4, "Great", "Fits well.", True, "published")
    again = await review(client, buyer, p["id"])
    assert again.status_code == 409 and again.json()["code"] == "already_reviewed"
    assert (await review(client, buyer, 999999)).status_code == 404


async def test_a_refunded_order_does_not_qualify_and_a_variant_purchase_counts_for_the_product(client, admin_headers, category, provider, test_session_factory):
    detail = await tee(client, admin_headers, category, status="active")
    buyer, _ = await signup(client)
    await buy(client, buyer, variant_id=detail["variants"][0]["id"], provider=provider)
    assert (await review(client, buyer, detail["id"])).status_code == 201                                             # bought a variant of it
    refunded, _ = await signup(client)
    simple = await make_product(client, admin_headers, category, stock=5, status="active")
    order = await buy(client, refunded, simple["id"], provider=provider, fulfil_with=admin_headers)
    assert (await review(client, refunded, simple["id"])).status_code == 201                                          # paid and shipped: qualifies
    await client.delete(f"{API}/products/{simple['id']}/reviews/mine", headers=refunded)
    assert (await client.post(f"{API}/payments/{order['id']}/refund", headers=admin_headers)).status_code == 202
    await jobs(test_session_factory)                                                                                  # the provider confirms the refund
    again = await review(client, refunded, simple["id"])
    assert again.status_code == 403 and again.json()["code"] == "review_requires_purchase"                            # a refunded order no longer counts


async def test_review_text_is_validated_cleaned_and_never_interpreted(client, admin_headers, category, provider):
    p = await make_product(client, admin_headers, category, stock=10, status="active")
    buyer, _ = await signup(client)
    await buy(client, buyer, p["id"], provider=provider)
    for bad in ({"rating": 0}, {"rating": 6}, {"rating": "five"}, {"title": "x" * 101}, {"body": "x" * 2001}, {"extra": 1}):
        assert (await review(client, buyer, p["id"], **bad)).status_code == 422, bad
    r = await review(client, buyer, p["id"], title="  Nice \x07  tee  ", body="Line one\n\nLine <b>two</b> & more \x00 ok")
    assert r.status_code == 201
    assert r.json()["title"] == "Nice tee" and r.json()["body"] == "Line one\n\nLine <b>two</b> & more ok"            # plain text: markup is kept as text
    only_rating = await client.patch(f"{API}/products/{p['id']}/reviews/mine", json={"title": None, "body": "   "}, headers=buyer)
    assert only_rating.json()["title"] is None and only_rating.json()["body"] is None and only_rating.json()["rating"] == 5
    assert (await client.patch(f"{API}/products/{p['id']}/reviews/mine", json={"rating": None}, headers=buyer)).status_code == 422


async def test_everyone_sees_published_reviews_with_masked_names_a_summary_and_batch_summaries(client, admin_headers, category, provider):
    p = await make_product(client, admin_headers, category, stock=20, status="active")
    q = await make_product(client, admin_headers, category, stock=20, status="active")
    ratings = [5, 4, 4, 1]
    for n, rating in enumerate(ratings):
        h, email = await signup(client)
        await buy(client, h, p["id"], provider=provider)
        await review(client, h, p["id"], rating=rating, title=f"Review {n}", body=None)
    listing = (await client.get(f"{API}/products/{p['id']}/reviews")).json()
    assert listing["total"] == 4 and [i["title"] for i in listing["items"]] == ["Review 3", "Review 2", "Review 1", "Review 0"]       # newest first
    assert all("@" not in i["display_name"] and "*" in i["display_name"] and i["status"] is None for i in listing["items"])             # no email, no status
    assert [i["rating"] for i in (await client.get(f"{API}/products/{p['id']}/reviews", params={"sort": "highest"})).json()["items"]] == [5, 4, 4, 1]
    assert [i["rating"] for i in (await client.get(f"{API}/products/{p['id']}/reviews", params={"sort": "lowest"})).json()["items"]][0] == 1
    page = (await client.get(f"{API}/products/{p['id']}/reviews", params={"page_size": 2, "page": 2})).json()
    assert len(page["items"]) == 2 and page["total"] == 4
    summary = (await client.get(f"{API}/products/{p['id']}/review-summary")).json()
    assert summary == {"product_id": p["id"], "count": 4, "average": 3.5, "distribution": {"1": 1, "2": 0, "3": 0, "4": 2, "5": 1}}
    batch = (await client.get(f"{API}/reviews/summaries", params=[("product_ids", p["id"]), ("product_ids", q["id"])])).json()
    assert [(b["product_id"], b["count"], b["average"]) for b in batch] == sorted([(p["id"], 4, 3.5), (q["id"], 0, None)])
    assert (await client.get(f"{API}/reviews/summaries")).status_code == 422                                                              # ids required
    draft = await make_product(client, admin_headers, category, status="draft")
    assert (await client.get(f"{API}/products/{draft['id']}/reviews")).status_code == 404                                                # not for sale: hidden


async def test_a_customer_can_edit_and_delete_their_own_review_only(client, admin_headers, category, provider):
    p = await make_product(client, admin_headers, category, stock=10, status="active")
    a, _ = await signup(client)
    await buy(client, a, p["id"], provider=provider)
    await review(client, a, p["id"], rating=3, title="OK", body="Fine.")
    edited = await client.patch(f"{API}/products/{p['id']}/reviews/mine", json={"rating": 5, "body": "Actually great."}, headers=a)
    assert (edited.json()["rating"], edited.json()["title"], edited.json()["body"]) == (5, "OK", "Actually great.")
    assert (await client.get(f"{API}/products/{p['id']}/reviews/mine", headers=a)).json()["rating"] == 5
    b, _ = await signup(client)
    assert (await client.get(f"{API}/products/{p['id']}/reviews/mine", headers=b)).status_code == 404                       # someone else's is not "mine"
    assert (await client.patch(f"{API}/products/{p['id']}/reviews/mine", json={"rating": 1}, headers=b)).status_code == 404
    assert (await client.delete(f"{API}/products/{p['id']}/reviews/mine", headers=a)).status_code == 204
    assert (await client.get(f"{API}/products/{p['id']}/review-summary")).json()["count"] == 0
    assert (await client.delete(f"{API}/products/{p['id']}/reviews/mine", headers=a)).status_code == 404
    assert (await review(client, a, p["id"], rating=2)).status_code == 201                                                  # and may review again


async def test_administrators_hide_publish_and_delete_reviews_with_a_reason_and_an_audit_trail(client, admin_headers, category, provider, db_session):
    p = await make_product(client, admin_headers, category, stock=10, status="active")
    a, _ = await signup(client)
    await buy(client, a, p["id"], provider=provider)
    rid = (await review(client, a, p["id"], rating=1, body="Rude words")).json()["id"]
    assert (await client.get(f"{API}/admin/reviews", headers=a)).status_code == 403
    listing = (await client.get(f"{API}/admin/reviews", params={"status": "published"}, headers=admin_headers)).json()
    assert listing["total"] == 1 and listing["items"][0]["user_id"] and listing["items"][0]["status"] == "published"
    no_reason = await client.patch(f"{API}/admin/reviews/{rid}", json={"status": "hidden"}, headers=admin_headers)
    assert no_reason.status_code == 422
    hidden = await client.patch(f"{API}/admin/reviews/{rid}", json={"status": "hidden", "reason": "abusive language"}, headers=admin_headers)
    assert hidden.json()["status"] == "hidden" and hidden.json()["hidden_reason"] == "abusive language"
    assert (await client.get(f"{API}/products/{p['id']}/reviews")).json()["total"] == 0                                    # gone for everyone
    assert (await client.get(f"{API}/products/{p['id']}/review-summary")).json()["count"] == 0
    mine = (await client.get(f"{API}/products/{p['id']}/reviews/mine", headers=a)).json()
    assert mine["status"] == "hidden"                                                                                         # the author can still see it
    blocked = await client.patch(f"{API}/products/{p['id']}/reviews/mine", json={"rating": 5}, headers=a)
    assert blocked.status_code == 403 and blocked.json()["code"] == "review_hidden"                                           # and cannot edit it back
    assert (await client.patch(f"{API}/admin/reviews/{rid}", json={"status": "published"}, headers=admin_headers)).json()["hidden_reason"] is None
    assert (await client.get(f"{API}/products/{p['id']}/reviews")).json()["total"] == 1
    assert (await client.delete(f"{API}/admin/reviews/{rid}", headers=admin_headers)).status_code == 204
    assert (await client.delete(f"{API}/admin/reviews/{rid}", headers=admin_headers)).status_code == 404
    await db_session.rollback()
    actions = {r[0] for r in (await db_session.execute(text("SELECT action FROM audit_logs"))).all()}
    assert {"review.hidden", "review.published", "review.deleted"} <= actions


async def test_two_simultaneous_reviews_by_one_customer_leave_one(client, admin_headers, category, provider, db_session):
    p = await make_product(client, admin_headers, category, stock=10, status="active")
    a, _ = await signup(client)
    await buy(client, a, p["id"], provider=provider)
    results = await asyncio.gather(*[review(client, a, p["id"]) for _ in range(3)])
    assert sorted(r.status_code for r in results) == [201, 409, 409]
    assert (await db_session.execute(text("SELECT count(*) FROM product_reviews"))).scalar_one() == 1


# ============================================================================================== wishlist
async def test_a_wishlist_holds_products_that_are_on_sale_and_adding_is_idempotent(client, admin_headers, category):
    a, _ = await signup(client)
    p1 = await make_product(client, admin_headers, category, stock=3, status="active", name="First")
    p2 = await make_product(client, admin_headers, category, stock=0, status="active", name="Second")
    draft = await make_product(client, admin_headers, category, status="draft")
    assert (await client.put(f"{API}/wishlist/{p1['id']}")).status_code in (401, 403)
    first = await client.put(f"{API}/wishlist/{p1['id']}", headers=a)
    assert first.json() == {"product_id": p1["id"], "in_wishlist": True, "count": 1}
    assert (await client.put(f"{API}/wishlist/{p1['id']}", headers=a)).json()["count"] == 1                                  # again: still one
    assert (await client.put(f"{API}/wishlist/{p2['id']}", headers=a)).json()["count"] == 2
    assert (await client.put(f"{API}/wishlist/{draft['id']}", headers=a)).status_code == 404                                 # not for sale
    assert (await client.put(f"{API}/wishlist/999999", headers=a)).status_code == 404
    listing = (await client.get(f"{API}/wishlist", headers=a)).json()
    assert [i["name"] for i in listing["items"]] == ["Second", "First"] and listing["total"] == 2                           # most recent first
    assert [(i["availability"], i["stock"]) for i in listing["items"]] == [("out_of_stock", None), ("low_stock", None)]       # shown like the catalog
    assert (await client.get(f"{API}/wishlist/ids", headers=a)).json() == [p2["id"], p1["id"]]
    assert (await client.delete(f"{API}/wishlist/{p1['id']}", headers=a)).json() == {"product_id": p1["id"], "in_wishlist": False, "count": 1}
    assert (await client.delete(f"{API}/wishlist/{p1['id']}", headers=a)).json()["count"] == 1                               # removing twice is fine


async def test_wishlists_are_private_and_hide_withdrawn_products(client, admin_headers, category):
    a, _ = await signup(client)
    b, _ = await signup(client)
    p = await make_product(client, admin_headers, category, stock=3, status="active")
    await client.put(f"{API}/wishlist/{p['id']}", headers=a)
    assert (await client.get(f"{API}/wishlist", headers=b)).json()["total"] == 0 and (await client.get(f"{API}/wishlist/ids", headers=b)).json() == []
    await client.delete(f"{API}/products/{p['id']}", headers=admin_headers)                                                  # withdrawn (archived)
    assert (await client.get(f"{API}/wishlist", headers=a)).json()["total"] == 0 and (await client.get(f"{API}/wishlist/ids", headers=a)).json() == []
    await client.patch(f"{API}/products/{p['id']}", headers=admin_headers, json={"status": "active"})
    assert (await client.get(f"{API}/wishlist/ids", headers=a)).json() == [p["id"]]                                          # it comes back with the product


async def test_a_wishlist_shows_a_product_with_options_like_the_catalog_does(client, admin_headers, category):
    detail = await tee(client, admin_headers, category, status="active")
    a, _ = await signup(client)
    await client.put(f"{API}/wishlist/{detail['id']}", headers=a)
    item = (await client.get(f"{API}/wishlist", headers=a)).json()["items"][0]
    assert (item["has_variants"], item["price_from_cents"], item["price_to_cents"], item["variant_count"]) == (True, 900, 1000, 4)


async def test_a_wishlist_is_limited_and_concurrent_adds_do_not_duplicate(client, admin_headers, category, db_session):
    a, _ = await signup(client)
    p = await make_product(client, admin_headers, category, stock=1, status="active")
    await asyncio.gather(*[client.put(f"{API}/wishlist/{p['id']}", headers=a) for _ in range(4)])
    assert (await db_session.execute(text("SELECT count(*) FROM wishlist_items"))).scalar_one() == 1
    uid = (await db_session.execute(text("SELECT user_id FROM wishlist_items"))).scalar_one()
    await db_session.execute(text("DELETE FROM wishlist_items"))
    await db_session.execute(text(
        "INSERT INTO products (name, slug, price_cents, sku, stock, status, category_id) "
        "SELECT 'W' || g, 'wl-' || g, 100, 'WL-' || g, 1, 'active', :c FROM generate_series(1, 200) AS g"), {"c": category})
    await db_session.execute(text("INSERT INTO wishlist_items (user_id, product_id) SELECT :u, id FROM products WHERE sku LIKE 'WL-%'"), {"u": uid})
    await db_session.commit()
    full = await client.put(f"{API}/wishlist/{p['id']}", headers=a)                                                          # the 201st
    assert full.status_code == 409 and full.json()["code"] == "wishlist_full"
    one_of_them = (await db_session.execute(text("SELECT product_id FROM wishlist_items LIMIT 1"))).scalar_one()
    assert (await client.put(f"{API}/wishlist/{one_of_them}", headers=a)).json()["count"] == 200                              # already saved: not an error
