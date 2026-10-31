# Error codes

Generated from the code by `python -m scripts.export_api_docs` (do not edit by hand). Every error body is `{"detail": ..., "request_id": ...}`; where the error has a stable code it also has `"code"`. Branch on `code`, never on the wording of `detail`. Errors without a code (a missing resource, an invalid token, not enough stock) are identified by their HTTP status.

| HTTP | code | meaning (default wording) | defined in |
|---|---|---|---|
| 400 | `current_password_incorrect` | The current password is not correct | `auth.exceptions.auth_exceptions.CurrentPasswordIncorrectError` |
| 400 | `invalid_or_expired_token` | This link is invalid or has expired. Request a new one. | `auth.exceptions.auth_exceptions.InvalidAccountTokenError` |
| 400 | `password_unchanged` | The new password must be different from the current one | `auth.exceptions.auth_exceptions.SamePasswordError` |
| 403 | `approval_requires_distinct_approver` | A record cannot be approved by the administrator who created it | `tax.exceptions.tax_exceptions.ApprovalRequiresDistinctApproverError` |
| 403 | `review_hidden` | This review was hidden by the store and can no longer be edited | `reviews.exceptions.review_exceptions.ReviewHiddenError` |
| 403 | `review_requires_purchase` | Only customers who bought this product (a paid or shipped order) can review it | `reviews.exceptions.review_exceptions.ReviewRequiresPurchaseError` |
| 404 | `coupon_not_found` | Coupon not found | `coupons.exceptions.coupon_exceptions.CouponNotFoundError` |
| 404 | `email_provider_not_configured` |  | `email_settings.exceptions.email_exceptions.EmailProviderNotFoundError` |
| 404 | `invoice_not_found` | No invoice has been issued for this order | `invoices.exceptions.invoice_exceptions.InvoiceNotFoundError` |
| 404 | `option_not_found` | Option or option value not found | `variants.exceptions.variant_exceptions.OptionNotFoundError` |
| 404 | `provider_config_not_found` | Payment provider configuration not found | `payments.exceptions.provider_exceptions.ProviderConfigNotFoundError` |
| 404 | `return_not_found` | Return not found | `returns.exceptions.return_exceptions.ReturnNotFoundError` |
| 404 | `review_not_found` | Review not found | `reviews.exceptions.review_exceptions.ReviewNotFoundError` |
| 404 | `shipment_not_found` | Shipment not found | `shipments.exceptions.shipment_exceptions.ShipmentNotFoundError` |
| 404 | `tax_record_not_found` |  | `tax.exceptions.tax_exceptions.TaxRecordNotFoundError` |
| 404 | `variant_not_found` | Variant not found | `variants.exceptions.variant_exceptions.VariantNotFoundError` |
| 409 | `already_has_variants` | This product already has options | `variants.exceptions.variant_exceptions.AlreadyHasVariantsError` |
| 409 | `already_reviewed` | You have already reviewed this product. Edit your review instead. | `reviews.exceptions.review_exceptions.AlreadyReviewedError` |
| 409 | `cancel_and_refund_not_allowed` |  | `orders.exceptions.order_exceptions.CancelAndRefundNotAllowedError` |
| 409 | `cancellation_not_allowed` | Only an unpaid (pending) order can be cancelled. A paid order needs a refund first, and the | `orders.exceptions.order_exceptions.CancellationNotAllowedError` |
| 409 | `cancellation_unconfirmed` | The order could not be cancelled yet because the payment provider could not confirm the payment state. Nothing was changed; this is being reconciled. Try again shortly. | `orders.exceptions.order_exceptions.CancellationUnconfirmedError` |
| 409 | `classification_exists` | A classification with this kind and code already exists | `tax.exceptions.tax_exceptions.DuplicateClassificationError` |
| 409 | `classification_in_use` | This classification cannot be retired: it still has draft or approved rules | `tax.exceptions.tax_exceptions.ClassificationInUseError` |
| 409 | `coupon_code_exists` | A coupon with this code already exists | `coupons.exceptions.coupon_exceptions.CouponCodeExistsError` |
| 409 | `coupon_in_use` | This coupon has been used by orders, so you cannot change that. Switch it off instead (active: false). | `coupons.exceptions.coupon_exceptions.CouponInUseError` |
| 409 | `currency_conflicts_with_payment` |  | `orders.exceptions.order_exceptions.CurrencyConflictsWithPaymentError` |
| 409 | `duplicate_tracking_number` | This tracking number is already recorded for this order and carrier | `shipments.exceptions.shipment_exceptions.DuplicateTrackingError` |
| 409 | `email_provider_active` | This email service is the active one. Switch to another service (or deactivate) before deleting it. | `email_settings.exceptions.email_exceptions.EmailProviderActiveError` |
| 409 | `idempotency_key_reused` | This Idempotency-Key was already used with a different request. Use a new key for a different checkout. | `orders.exceptions.order_exceptions.IdempotencyKeyConflictError` |
| 409 | `invalid_shipment_status_change` |  | `shipments.exceptions.shipment_exceptions.InvalidShipmentStatusChangeError` |
| 409 | `last_admin` | This is the last active administrator: demoting or deactivating it would leave nobody able to administer the store. Promote or create another administrator first. | `users.exceptions.user_exceptions.LastAdminError` |
| 409 | `option_value_in_use` | A variant uses this value, so it cannot be removed | `variants.exceptions.variant_exceptions.OptionValueInUseError` |
| 409 | `options_locked` | This product already has variants, so its option sets cannot be redefined. Add values to an option instead. | `variants.exceptions.variant_exceptions.OptionsLockedError` |
| 409 | `order_currency_known` | This order already has a recorded currency, which can never be changed | `orders.exceptions.order_exceptions.OrderCurrencyAlreadyKnownError` |
| 409 | `order_not_shippable` |  | `shipments.exceptions.shipment_exceptions.OrderNotShippableError` |
| 409 | `payment_in_progress` | A payment for this order is already in progress or completed, so it cannot be cancelled | `orders.exceptions.order_exceptions.PaymentInProgressError` |
| 409 | `product_has_open_orders` |  | `variants.exceptions.variant_exceptions.ProductHasOpenOrdersError` |
| 409 | `product_has_stock` | This product holds stock of its own. Use POST /products/{id}/convert-to-variants to turn it into a product with options. | `variants.exceptions.variant_exceptions.ProductHasStockError` |
| 409 | `product_in_use` | This product is already in carts or orders. Use POST /products/{id}/convert-to-variants to turn it into a product with options. | `variants.exceptions.variant_exceptions.ProductInUseError` |
| 409 | `product_needs_active_variant` | An active product with options needs at least one active variant | `variants.exceptions.variant_exceptions.ProductNeedsActiveVariantError` |
| 409 | `provider_not_ready` | Activation refused: lists the safe reasons (never secrets). | `payments.exceptions.provider_exceptions.ProviderNotReadyError` |
| 409 | `refund_already_requested` |  | `payments.exceptions.payment_exceptions.RefundAlreadyRequestedError` |
| 409 | `registration_exists` | A registration with this GSTIN already exists | `tax.exceptions.tax_exceptions.DuplicateRegistrationError` |
| 409 | `registration_in_use` | This registration is used by a tax configuration that is not retired | `tax.exceptions.tax_exceptions.RegistrationInUseError` |
| 409 | `return_invalid_state` |  | `returns.exceptions.return_exceptions.ReturnStateError` |
| 409 | `return_not_full` | NovaCart refunds a whole order at a time, and this return does not bring back every unit of it. Close the return with the resolution 'refunded_outside' once you have refunded the customer at your payment provider, or 'replacement_sent' / 'no_refund'. | `returns.exceptions.return_exceptions.ReturnNotFullError` |
| 409 | `rule_overlap` | An approved rule already applies to this classification in that period | `tax.exceptions.tax_exceptions.RuleOverlapError` |
| 409 | `settings_overlap` | An approved tax configuration already applies in that period. Retire it first, then approve the new one. | `tax.exceptions.tax_exceptions.SettingsOverlapError` |
| 409 | `sku_already_exists` |  | `variants.exceptions.variant_exceptions.SkuInUseError` |
| 409 | `status_change_not_allowed` | An administrator asked for a status change that would bypass payment, stock or refund handling. | `orders.exceptions.order_exceptions.StatusChangeNotAllowedError` |
| 409 | `stock_changed` | The product's stock is not what the administrator allocated against: nothing was changed, review and send again. | `variants.exceptions.variant_exceptions.StockChangedError` |
| 409 | `store_currency_locked` | The store currency is fixed once it has been set and cannot be changed. Orders keep the currency they were placed in. | `store.exceptions.store_exceptions.StoreCurrencyLockedError` |
| 409 | `tax_master_immutable` | Only draft records can be edited. Approved records are closed or retired and replaced by a new record. | `tax.exceptions.tax_exceptions.ImmutableTaxRecordError` |
| 409 | `tax_master_invalid_transition` |  | `tax.exceptions.tax_exceptions.InvalidTaxTransitionError` |
| 409 | `variant_combination_exists` | That combination already exists | `variants.exceptions.variant_exceptions.VariantCombinationExistsError` |
| 409 | `variant_in_use` | This variant has stock history, or is in a cart or an order, so it cannot be deleted. Set it to inactive instead. | `variants.exceptions.variant_exceptions.VariantInUseError` |
| 409 | `wishlist_full` |  | `reviews.exceptions.review_exceptions.WishlistFullError` |
| 422 | `approval_prerequisite_missing` |  | `tax.exceptions.tax_exceptions.ApprovalPrerequisiteError` |
| 422 | `business_profile_incomplete` | A GST invoice cannot be issued until the business profile (address, state, invoice prefix) is set: PUT /admin/business-profile | `invoices.exceptions.invoice_exceptions.BusinessProfileIncompleteError` |
| 422 | `classification_not_approved` |  | `products.exceptions.product_exceptions.ClassificationNotApprovedError` |
| 422 | `coupon_invalid` | This coupon code is not valid | `coupons.exceptions.coupon_exceptions.CouponInvalidError` |
| 422 | `coupon_limit_reached` | This coupon has been used the maximum number of times | `coupons.exceptions.coupon_exceptions.CouponLimitReachedError` |
| 422 | `coupon_minimum_not_met` |  | `coupons.exceptions.coupon_exceptions.CouponMinimumNotMetError` |
| 422 | `coupon_not_applicable` | This coupon does not apply to your order | `coupons.exceptions.coupon_exceptions.CouponNotApplicableError` |
| 422 | `coupon_settings_invalid` | Invalid request | `coupons.exceptions.coupon_exceptions.CouponSettingsInvalidError` |
| 422 | `currency_unsupported` |  | `core.money.UnsupportedCurrencyError` |
| 422 | `delivery_country_not_supported` | Delivery outside India is not supported | `tax.engine.DeliveryCountryNotSupportedError` |
| 422 | `delivery_not_available` | We cannot deliver to this address. | `orders.exceptions.order_exceptions.DeliveryNotAvailableError` |
| 422 | `delivery_state_unknown` | We could not recognise the delivery state. Please choose the state from the list. | `tax.engine.DeliveryStateUnknownError` |
| 422 | `email_provider_test_failed` | The provider's connection test failed. Carries only a fixed error code and its fixed explanation, never server text. | `email_settings.exceptions.email_exceptions.EmailProviderTestFailedError` |
| 422 | `evidence_not_specific_to_order` | The evidence does not demonstrably concern THIS order, so it cannot establish its currency. | `orders.exceptions.order_exceptions.EvidenceNotSpecificToOrderError` |
| 422 | `invalid_email_provider_settings` | Invalid request | `email_settings.exceptions.email_exceptions.InvalidEmailProviderSettingsError` |
| 422 | `invalid_money_amount` | Invalid request | `core.money.InvalidMoneyError` |
| 422 | `invalid_report_range` | Invalid request | `reports.exceptions.report_exceptions.InvalidReportRangeError` |
| 422 | `invalid_review` | Invalid request | `reviews.exceptions.review_exceptions.InvalidReviewError` |
| 422 | `invalid_variant_spec` | Invalid request | `variants.exceptions.variant_exceptions.InvalidVariantSpecError` |
| 422 | `invoice_not_applicable` | Invalid request | `invoices.exceptions.invoice_exceptions.InvoiceNotApplicableError` |
| 422 | `nothing_returnable` | Everything in this order is already part of a return request | `returns.exceptions.return_exceptions.NothingReturnableError` |
| 422 | `order_currency_unknown` | This order has no recorded currency, so it cannot be refunded or paid. An administrator must establish its currency from reliable records first (GET /admin/orders/unknown-currency). | `payments.exceptions.payment_exceptions.OrderCurrencyUnknownError` |
| 422 | `order_not_shipped` | Only an order that has been shipped can be returned | `returns.exceptions.return_exceptions.OrderNotShippedError` |
| 422 | `partial_refund_not_supported` | Part of this payment has already been refunded at the provider. NovaCart initiates full refunds only: complete the remaining refund in the provider's dashboard, where it will be recorded here. | `payments.exceptions.payment_exceptions.PartialRefundNotSupportedError` |
| 422 | `payment_disputed` | This payment has an open dispute and cannot be refunded until it is resolved | `payments.exceptions.payment_exceptions.PaymentDisputedError` |
| 422 | `payment_inconsistent` | The payment does not match the order | `payments.exceptions.payment_exceptions.PaymentInconsistentError` |
| 422 | `product_not_convertible` | Invalid request | `variants.exceptions.variant_exceptions.ProductNotConvertibleError` |
| 422 | `product_tax_classification_required` | An active product needs an approved tax classification | `products.exceptions.product_exceptions.ProductTaxRequiredError` |
| 422 | `product_tax_unassigned` |  | `orders.exceptions.order_exceptions.ProductTaxUnassignedError` |
| 422 | `reconciliation_not_open` | This reconciliation record does not exist or is already resolved | `payments.exceptions.payment_exceptions.ReconciliationNotOpenError` |
| 422 | `return_item_invalid` | Invalid request | `returns.exceptions.return_exceptions.ReturnItemInvalidError` |
| 422 | `return_quantity_exceeded` |  | `returns.exceptions.return_exceptions.ReturnQuantityExceededError` |
| 422 | `return_receipt_invalid` | Invalid request | `returns.exceptions.return_exceptions.ReturnReceiptInvalidError` |
| 422 | `return_window_closed` |  | `returns.exceptions.return_exceptions.ReturnWindowClosedError` |
| 422 | `returns_disabled` | This store does not accept online return requests. Please contact the store. | `returns.exceptions.return_exceptions.ReturnsDisabledError` |
| 422 | `self_approval_note_required` | Self-approval requires a written note | `tax.exceptions.tax_exceptions.SelfApprovalNoteRequiredError` |
| 422 | `stock_allocation_mismatch` |  | `variants.exceptions.variant_exceptions.StockAllocationMismatchError` |
| 422 | `stock_reason_required` | Changing stock needs a reason (stock_reason, at least 3 characters) | `products.exceptions.product_exceptions.StockReasonRequiredError` |
| 422 | `stock_would_be_negative` |  | `variants.exceptions.variant_exceptions.StockWouldBeNegativeError` |
| 422 | `store_currency_not_set` | The store currency is not set, so money totals cannot be reported. Set it first (PUT /store/currency). | `reports.exceptions.report_exceptions.ReportCurrencyUnknownError` |
| 422 | `store_history_ambiguous` | Store configuration history alone is not enough when it is ambiguous. | `orders.exceptions.order_exceptions.StoreHistoryAmbiguousError` |
| 422 | `tax_calculation_blocked` | Tax cannot be determined for some items; nothing is assumed or defaulted. | `tax.engine.TaxCalculationBlockedError` |
| 422 | `tax_classification_invalid` |  | `products.exceptions.product_exceptions.InvalidTaxClassificationError` |
| 422 | `tax_master_invalid_input` | Invalid request | `tax.exceptions.tax_exceptions.InvalidTaxInputError` |
| 422 | `use_variant_stock` | This product has options: its stock lives on its variants. Adjust a variant's stock instead. | `variants.exceptions.variant_exceptions.UseVariantStockError` |
| 422 | `variant_required` | This product comes in options: choose a variant (send variant_id) | `cart.exceptions.cart_exceptions.VariantRequiredError` |
| 422 | `variant_unavailable` | This option is not available | `cart.exceptions.cart_exceptions.VariantUnavailableError` |
| 503 | `checkout_not_configured` | Checkout is temporarily unavailable: shipping and tax are not configured. | `orders.exceptions.order_exceptions.CheckoutNotConfiguredError` |
| 503 | `live_payments_disabled` | Live payments are disabled | `payments.exceptions.payment_exceptions.LivePaymentsDisabledError` |
| 503 | `secret_storage_not_configured` | Credential storage is not configured on this deployment (PROVIDER_SECRETS_KEY is not set) | `core.secrets.SecretStorageNotConfiguredError` |
| 503 | `secret_unreadable` | A stored credential cannot be decrypted with the configured keys | `core.secrets.SecretUnreadableError` |
| 503 | `store_currency_not_configured` | The store currency is not configured | `core.money.StoreCurrencyNotConfiguredError` |
| 503 | `tax_engine_not_implemented` | GST calculation is not available yet | `tax.engine.TaxEngineUnavailableError` |
| 503 | `tax_not_configured` | Tax calculation is not configured | `tax.engine.TaxNotConfiguredError` |

111 codes.
