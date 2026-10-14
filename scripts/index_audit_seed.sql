SELECT setseed(0.42);
INSERT INTO categories (name, slug, description, is_active, created_at, updated_at)
SELECT 'Category '||g, 'category-'||g, NULL, true, now(), now() FROM generate_series(1,50) g;

INSERT INTO users (id, username, email, password_hash, role, is_active, created_at, updated_at)
SELECT gen_random_uuid(), 'user'||g, 'user'||g||'@example.com', 'x', CASE WHEN g<=5 THEN 'admin'::user_role ELSE 'user'::user_role END,
       true, now() - (random()*730||' days')::interval, now()
FROM generate_series(1,100000) g;

INSERT INTO products (name, slug, description, price_cents, sku, stock, status, category_id, created_at, updated_at)
SELECT (ARRAY['Wireless','Compact','Premium','Classic','Smart','Portable','Pro','Mini'])[1+(g%8)] || ' ' ||
       (ARRAY['Camera','Headphones','Keyboard','Monitor','Backpack','Lamp','Speaker','Router','Charger','Tripod'])[1+(g%10)] || ' ' || g,
       'product-'||g, NULL, 100 + (random()*99900)::int, 'SKU-'||g, (random()*500)::int,
       CASE WHEN random()<0.85 THEN 'active'::product_status WHEN random()<0.67 THEN 'draft'::product_status ELSE 'archived'::product_status END,
       1 + (random()*49)::int, now() - (random()*730||' days')::interval, now()
FROM generate_series(1,200000) g;

CREATE TEMP TABLE u AS SELECT id, row_number() OVER () rn FROM users;
CREATE INDEX ON u(rn);
INSERT INTO orders (id, user_id, status, idempotency_key, subtotal_cents, shipping_cents, tax_cents, total_cents, created_at, updated_at,
                    guest_token, guest_email)
SELECT gen_random_uuid(),
       CASE WHEN g%10<8 THEN (SELECT id FROM u WHERE rn = 1 + (g::bigint*7919)%100000) END,
       CASE WHEN r<0.03 THEN 'pending'::order_status WHEN r<0.33 THEN 'paid'::order_status WHEN r<0.88 THEN 'fulfilled'::order_status
            WHEN r<0.96 THEN 'cancelled'::order_status ELSE 'refunded'::order_status END,
       'key-'||g, 5000, 500, 400, 5900, ts, ts,
       CASE WHEN g%10>=8 THEN 'guest-token-'||g END, CASE WHEN g%10>=8 THEN 'guest'||g||'@example.com' END
FROM (SELECT g, random() r, now() - (random()*730||' days')::interval ts FROM generate_series(1,300000) g) s;

INSERT INTO order_items (order_id, product_id, product_name_snapshot, price_cents_snapshot, quantity, created_at, updated_at)
SELECT o.id, 1 + ((n*104729 + extract(epoch from o.created_at)::bigint) % 200000)::int, 'snapshot', 2500, 1+(n%3), o.created_at, o.created_at
FROM orders o, generate_series(1,2) n;

INSERT INTO payments (id, order_id, provider, provider_payment_id, status, amount_cents, currency, created_at, updated_at)
SELECT gen_random_uuid(), id, 'stripe', 'pi_'||replace(id::text,'-',''),
       CASE status WHEN 'pending' THEN 'pending'::payment_status WHEN 'cancelled' THEN 'failed'::payment_status
                   WHEN 'refunded' THEN 'refunded'::payment_status ELSE 'succeeded'::payment_status END,
       total_cents, 'usd', created_at, created_at FROM orders;

INSERT INTO audit_logs (actor_user_id, action, target_type, target_id, details, created_at, updated_at)
SELECT (SELECT id FROM u WHERE rn = 1 + (g%5)), (ARRAY['product.created','product.updated','order.status_changed','category.created','user.updated'])[1+(g%5)],
       (ARRAY['product','product','order','category','user'])[1+(g%5)], g::text, '{}'::jsonb,
       now() - (random()*730||' days')::interval, now()
FROM generate_series(1,200000) g;

INSERT INTO refresh_tokens (id, jti, user_id, expires_at, revoked, created_at, updated_at, family_id)
SELECT gen_random_uuid(), gen_random_uuid()::text, (SELECT id FROM u WHERE rn = 1 + (g::bigint*31)%100000), now() + interval '7 days',
       random()<0.5, now(), now(), gen_random_uuid()
FROM generate_series(1,200000) g;

INSERT INTO carts (user_id, created_at, updated_at) SELECT id, now(), now() FROM u WHERE rn <= 50000;
INSERT INTO cart_items (cart_id, product_id, quantity, price_cents_snapshot, created_at, updated_at)
SELECT c.id, 1 + ((c.id*31 + n*7907) % 200000), 1, 1000, now(), now() FROM carts c, generate_series(1,3) n;

INSERT INTO addresses (user_id, recipient_name, line1, city, state, postal_code, country, is_default, created_at, updated_at)
SELECT id, 'R', '1 St', 'City', 'ST', '00000', 'US', true, now(), now() FROM u;

INSERT INTO order_status_history (order_id, from_status, to_status, created_at, updated_at)
SELECT id, 'pending', 'paid', created_at, created_at FROM orders WHERE status <> 'pending';
ANALYZE;
