# Staff roles and permissions (BE-P1)

Source of truth: `app/modules/roles/permissions.py`. Every administrator-only route declares one permission; `tests/roles/test_permission_coverage.py` fails if a route takes `AdminUser` without one.

## Rules

- Only an active administrator (`users.role = admin`) holds permissions. A customer holds none and still gets the old 403 (no `code`) on staff routes.
- One role per administrator. **Owner** (system) holds every permission, including future ones. **No access** (system) holds none. System roles cannot be edited or deleted (422 `role_is_system`).
- An administrator with no role row (created directly in the database, e.g. the seed or a bootstrap owner) holds every permission. Migration `c5e1a7f3b964` gives every existing administrator Owner.
- Promotion through `PATCH /users/{id}` (`role: admin`) assigns **No access**: access is granted only by assigning a role (`PUT /admin/staff/{user_id}/role`). Demotion removes the role row.
- A change permission includes its read (`IMPLIES`): catalogue.write and inventory.adjust include catalogue.read; customers.manage includes customers.read; orders.manage includes orders.read; returns.manage includes returns.read and orders.read; refunds.issue includes orders.read, returns.read and payments.read; payments.manage/credentials/live include payments.read; tax.configure includes tax.read; content.publish includes content.edit.
- Refused: 403 `permission_denied` with `permission`; 409 `last_role_manager` if no active administrator would keep roles.manage (role change, role edit, demotion, deactivation); 409 `role_in_use` with `members`; 409 `role_name_taken` (case-insensitive); 404 `role_not_found`, `staff_member_not_found`.
- Every role and assignment change is audited: `staff_role.created|updated|deleted`, `staff.role_assigned`.

## Endpoints

`GET /access/me` (any signed-in user, `no-store`): `is_staff`, `role` (null for a customer or a role-less administrator), effective `permissions`. UI shaping only; the backend checks every request.

`GET /admin/permissions`, `GET|POST /admin/roles`, `GET|PATCH|DELETE /admin/roles/{id}` (`RoleRead` has `permissions` as given and `effective_permissions`), `GET /admin/staff`, `PUT /admin/staff/{user_id}/role`: all roles.manage.

## Which permission guards what

| Permission | Endpoints |
| --- | --- |
| `reports.read` | `GET /admin/reports/low-stock`; `GET /admin/reports/sales-by-day`; `GET /admin/reports/summary`; `GET /admin/reports/top-products` |
| `audit.read` | `GET /audit-logs` |
| `catalogue.read` | `GET /products/{product_id}/stock-history`; `GET /products/{product_id}/variants/missing-combinations`; draft and archived products, exact stock (`GET /products`, `GET /products/{id}`) |
| `catalogue.write` | `DELETE /categories/{category_id}/image`; `DELETE /categories/{category_id}`; `DELETE /products/{product_id}/image`; `DELETE /products/{product_id}/option-values/{value_id}`; `DELETE /products/{product_id}/variants/{variant_id}`; `DELETE /products/{product_id}`; `PATCH /categories/{category_id}`; `PATCH /products/{product_id}/option-values/{value_id}`; `PATCH /products/{product_id}/variants/{variant_id}`; `PATCH /products/{product_id}`; `POST /categories/{category_id}/image`; `POST /categories`; `POST /products/{product_id}/convert-to-variants`; `POST /products/{product_id}/image`; `POST /products/{product_id}/options/{option_id}/values`; `POST /products/{product_id}/variants/bulk`; `POST /products/{product_id}/variants`; `POST /products`; `PUT /products/{product_id}/options` |
| `inventory.adjust` | `POST /products/{product_id}/stock`; `POST /products/{product_id}/variants/{variant_id}/stock` |
| `reviews.moderate` | `DELETE /admin/reviews/{review_id}`; `GET /admin/reviews`; `PATCH /admin/reviews/{review_id}` |
| `customers.read` |  |
| `customers.manage` |  |
| `orders.read` | `GET /admin/invoices`; `GET /orders/admin`; any customer's `GET /orders/{id}` and its invoice/credit note (read and print) |
| `orders.manage` | `DELETE /orders/{order_id}/shipments/{shipment_id}`; `PATCH /orders/{order_id}/shipments/{shipment_id}`; `PATCH /orders/{order_id}/status`; `POST /orders/{order_id}/invoice`; `POST /orders/{order_id}/shipments`; cancelling any customer's unpaid order (`POST /orders/{id}/cancel`) |
| `returns.read` | `GET /admin/returns/{return_id}`; `GET /admin/returns` |
| `returns.manage` | `POST /admin/returns/{return_id}/approve`; `POST /admin/returns/{return_id}/close`; `POST /admin/returns/{return_id}/receive`; `POST /admin/returns/{return_id}/reject` |
| `refunds.issue` | `POST /admin/orders/{order_id}/cancel-and-refund`; `POST /admin/reconciliations/{record_id}/refund`; `POST /admin/returns/{return_id}/refund`; `POST /payments/{order_id}/refund` |
| `payments.read` | `GET /admin/disputes`; `GET /admin/health/dependencies`; `GET /admin/lifecycle/health`; `GET /admin/orders/unknown-currency`; `GET /admin/payment-providers/catalog`; `GET /admin/payment-providers/{config_id}/readiness`; `GET /admin/payment-providers/{config_id}/webhook`; `GET /admin/payment-providers/{config_id}`; `GET /admin/payment-providers`; `GET /admin/payments/overview`; `GET /admin/reconciliations`; `GET /admin/refunds`; `GET /admin/webhook-events/deferred`; `GET /admin/webhook-events` |
| `payments.manage` | `POST /admin/orders/{order_id}/establish-currency`; `POST /admin/payment-providers/{config_id}/check`; `POST /admin/payment-providers/{config_id}/disable`; `POST /admin/payment-providers/{config_id}/enable`; `POST /admin/payment-providers/{config_id}/prefer`; `POST /admin/payment-providers/{config_id}/validate`; `POST /admin/payments/demo/disable`; `POST /admin/payments/demo/enable`; `POST /admin/reconciliations/{record_id}/resolve`; `PUT /admin/payment-providers/{config_id}/currencies` |
| `payments.credentials` | `POST /admin/payment-providers/{config_id}/webhook-secret/retire-previous`; `POST /admin/payment-providers`; `PUT /admin/payment-providers/{config_id}/credentials`; `PUT /admin/payment-providers/{config_id}/publishable-key` |
| `payments.live` | `POST /admin/payment-providers/{config_id}/attestations`; `POST /admin/payments/live/activate`; `POST /admin/payments/live/deactivate` |
| `tax.read` | `GET /admin/business-profile`; `GET /admin/tax/classifications`; `GET /admin/tax/products`; `GET /admin/tax/readiness`; `GET /admin/tax/registrations`; `GET /admin/tax/rules`; `GET /admin/tax/settings` |
| `tax.configure` | `PATCH /admin/tax/classifications/{record_id}`; `PATCH /admin/tax/registrations/{record_id}`; `PATCH /admin/tax/rules/{record_id}`; `PATCH /admin/tax/settings/{record_id}`; `POST /admin/tax/classifications/{record_id}/approve`; `POST /admin/tax/classifications/{record_id}/retire`; `POST /admin/tax/classifications`; `POST /admin/tax/registrations/{record_id}/approve`; `POST /admin/tax/registrations/{record_id}/retire`; `POST /admin/tax/registrations`; `POST /admin/tax/rules/{record_id}/approve`; `POST /admin/tax/rules/{record_id}/close`; `POST /admin/tax/rules/{record_id}/retire`; `POST /admin/tax/rules`; `POST /admin/tax/settings/{record_id}/approve`; `POST /admin/tax/settings/{record_id}/retire`; `POST /admin/tax/settings`; `PUT /admin/business-profile` |
| `store.configure` | `PUT /store/currency` |
| `coupons.manage` | `DELETE /admin/coupons/{coupon_id}`; `GET /admin/coupons/{coupon_id}/redemptions`; `GET /admin/coupons/{coupon_id}`; `GET /admin/coupons`; `PATCH /admin/coupons/{coupon_id}`; `POST /admin/coupons` |
| `email.configure` | `DELETE /admin/email-providers/{provider}`; `GET /admin/email-providers/catalog`; `GET /admin/email-providers`; `POST /admin/email-providers/deactivate`; `POST /admin/email-providers/{provider}/activate`; `POST /admin/email-providers/{provider}/test-email`; `POST /admin/email-providers/{provider}/test`; `PUT /admin/email-providers/{provider}` |
| `content.edit` | `DELETE /admin/site-config/draft`; `GET /admin/site-config/versions/{version}`; `GET /admin/site-config/versions`; `GET /admin/site-config`; `PUT /admin/site-config/draft` |
| `content.publish` | `POST /admin/site-config/publish`; `POST /admin/site-config/rollback/{version}` |
| `roles.manage` | `DELETE /admin/roles/{role_id}`; `GET /admin/permissions`; `GET /admin/roles/{role_id}`; `GET /admin/roles`; `GET /admin/staff`; `PATCH /admin/roles/{role_id}`; `POST /admin/roles`; `PUT /admin/staff/{user_id}/role`; changing anyone's role through `PATCH /users/{id}` |

Step-up re-authentication (BE-P2) is not part of this change: it will be required for payments.live, payments.credentials, roles.manage and refunds.issue actions.
