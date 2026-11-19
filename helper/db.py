import hashlib
import secrets
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

LEASE_SECONDS = 100


class Conflict(Exception):
    pass


class Database:
    def __init__(self, dsn, cipher):
        self.dsn = dsn
        self.cipher = cipher

    @contextmanager
    def cursor(self):
        import psycopg2
        from psycopg2.extras import RealDictCursor

        conn = psycopg2.connect(self.dsn, connect_timeout=5)
        try:
            with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
                yield cur
        finally:
            conn.close()

    def initialize(self):
        with self.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(544422651)")
            cur.execute(Path(__file__).with_name("schema.sql").read_text())

    def create(self, user_id, settings):
        from psycopg2.errors import UniqueViolation

        try:
            with self.cursor() as cur:
                cur.execute("""
                  INSERT INTO helper_orders
                    (user_id,server_id,server_name,nickname,password_cipher,keyword,
                     min_price,max_price,mode,interval_seconds)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
                """, (user_id, *(settings[key] for key in (
                    "server_id", "server_name", "nickname", "password_cipher", "keyword",
                    "min_price", "max_price", "mode", "interval_seconds",
                ))))
                return cur.fetchone()["id"]
        except UniqueViolation:
            raise Conflict("Для этого аккаунта на выбранном сервере уже есть активный заказ.") from None

    @staticmethod
    def _summary():
        return """o.id, o.server_name, o.nickname, o.keyword, o.min_price, o.max_price,
                  o.mode, o.interval_seconds, o.status, o.stop_requested, o.partial_results, o.error,
                  (SELECT count(*) FROM helper_lots l WHERE l.order_id=o.id) AS found,
                  (SELECT count(*) FROM helper_lots l WHERE l.order_id=o.id AND l.purchase='bought') AS bought"""

    def orders(self, user_id, before, limit=8):
        with self.cursor() as cur:
            cur.execute(f"""
              SELECT {self._summary()} FROM helper_orders o
                WHERE o.user_id=%s AND o.id<%s ORDER BY o.id DESC LIMIT %s
            """, (user_id, before, limit))
            return cur.fetchall()

    def order(self, user_id, order_id):
        with self.cursor() as cur:
            cur.execute(f"SELECT {self._summary()} FROM helper_orders o WHERE o.user_id=%s AND o.id=%s",
                        (user_id, order_id))
            return cur.fetchone()

    def stop(self, user_id, order_id):
        with self.cursor() as cur:
            cur.execute("""
              UPDATE helper_orders SET stop_requested=TRUE, updated_at=now(),
                status=CASE WHEN status='queued' THEN 'stopped' ELSE status END,
                password_cipher=CASE WHEN status='queued' THEN NULL ELSE password_cipher END
              WHERE user_id=%s AND id=%s AND status IN ('queued','running')
              RETURNING status
            """, (user_id, order_id))
            return cur.fetchone()

    def enroll(self, label):
        token = secrets.token_urlsafe(36)
        worker = str(uuid4())
        with self.cursor() as cur:
            cur.execute("INSERT INTO helper_workers(id,label,token_hash) VALUES (%s,%s,%s)",
                        (worker, label, hashlib.sha256(token.encode()).digest()))
        return worker, token

    def disable(self, worker):
        with self.cursor() as cur:
            cur.execute("UPDATE helper_workers SET enabled=FALSE WHERE id=%s", (worker,))

    def workers(self):
        with self.cursor() as cur:
            cur.execute("""
              SELECT w.label, w.enabled, w.seen_at > now()-interval '35 seconds' AS online,
                     o.id AS order_id
                FROM helper_workers w LEFT JOIN helper_orders o ON o.worker_id=w.id AND o.status='running'
                ORDER BY w.seen_at DESC NULLS LAST, w.label LIMIT 20
            """)
            return cur.fetchall()

    @staticmethod
    def _worker(cur, token):
        cur.execute("""
            UPDATE helper_workers SET seen_at=now() WHERE token_hash=%s AND enabled RETURNING id
        """, (hashlib.sha256(token.encode()).digest(),))
        worker = cur.fetchone()
        if not worker:
            raise PermissionError
        return worker["id"]

    def claim(self, token):
        with self.cursor() as cur:
            worker = self._worker(cur, token)
            cur.execute("""
                SELECT 1 FROM helper_orders WHERE worker_id=%s AND status='running'
            """, (worker,))
            if cur.fetchone():
                return None
            cur.execute("""
              WITH take AS (
                SELECT id FROM helper_orders WHERE status='queued' AND NOT stop_requested
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
              )
              UPDATE helper_orders o SET status='running', worker_id=%s,
                  lease_until=now()+make_interval(secs => %s), updated_at=now()
                FROM take WHERE take.id=o.id
                RETURNING o.id,o.server_id,o.nickname,o.password_cipher,o.keyword,
                          o.min_price,o.max_price,o.mode,o.interval_seconds
            """, (worker, LEASE_SECONDS))
            row = cur.fetchone()
            if row is None:
                return None
            job = dict(row)
            job["password"] = self.cipher.decrypt(bytes(job.pop("password_cipher"))).decode()
            # A live job never reconnects automatically. The worker receives the
            # password once, and the encrypted copy is deleted in this transaction.
            cur.execute("UPDATE helper_orders SET password_cipher=NULL WHERE id=%s", (job["id"],))
            return job

    def pulse(self, token, order_id, partial=None):
        with self.cursor() as cur:
            worker = self._worker(cur, token)
            cur.execute("""
              UPDATE helper_orders SET lease_until=now()+make_interval(secs => %s),
                partial_results=COALESCE(%s,partial_results)
              WHERE id=%s AND worker_id=%s AND status='running' AND lease_until>now()
              RETURNING stop_requested
            """, (LEASE_SECONDS, partial, order_id, worker))
            row = cur.fetchone()
            return not row or row["stop_requested"]

    @staticmethod
    def _locked_order(cur, worker, order_id):
        cur.execute("""
          SELECT mode,stop_requested,min_price,max_price FROM helper_orders
            WHERE id=%s AND worker_id=%s AND status='running' AND lease_until>now() FOR UPDATE
        """, (order_id, worker))
        row = cur.fetchone()
        if not row:
            raise Conflict("Заказ больше не закреплён за воркером.")
        return row

    def observe(self, token, order_id, lot_id, name, price):
        with self.cursor() as cur:
            worker = self._worker(cur, token)
            order = self._locked_order(cur, worker, order_id)
            if order["stop_requested"] or not order["min_price"] <= price <= order["max_price"]:
                return False
            cur.execute("""
                INSERT INTO helper_lots(order_id,lot_id,name,price) VALUES (%s,%s,%s,%s)
                ON CONFLICT DO NOTHING RETURNING lot_id
            """, (order_id, lot_id, name, price))
            created = cur.fetchone() is not None
            if created and order["mode"] == "notify":
                amount = f"{price:,}".replace(",", " ")
                cur.execute("INSERT INTO helper_alerts(order_id,text) VALUES (%s,%s)",
                            (order_id, f"🔔 Заказ #{order_id}. Найдено: {name} за {amount} ₽. Лот #{lot_id}."))
            return created

    def reserve(self, token, order_id, lot_id, price):
        with self.cursor() as cur:
            worker = self._worker(cur, token)
            order = self._locked_order(cur, worker, order_id)
            if (order["stop_requested"] or order["mode"] != "buy"
                    or not order["min_price"] <= price <= order["max_price"]):
                return False
            cur.execute("""
                UPDATE helper_lots SET purchase='pending',price=%s
                WHERE order_id=%s AND lot_id=%s AND purchase IS NULL RETURNING lot_id
            """, (price, order_id, lot_id))
            return cur.fetchone() is not None

    def purchase(self, token, order_id, lot_id, outcome):
        with self.cursor() as cur:
            worker = self._worker(cur, token)
            self._locked_order(cur, worker, order_id)
            cur.execute("""
                UPDATE helper_lots SET purchase=%s WHERE order_id=%s AND lot_id=%s AND purchase='pending'
                RETURNING name, price
            """, (outcome, order_id, lot_id))
            row = cur.fetchone()
            if row and outcome == "bought":
                amount = f"{row['price']:,}".replace(",", " ")
                cur.execute("INSERT INTO helper_alerts(order_id,text) VALUES (%s,%s)",
                            (order_id, f"🛒 Заказ #{order_id}. Куплен лот «{row['name']}» за {amount} ₽. Забери его в пункте выдачи."))

    def finish(self, token, order_id, error):
        with self.cursor() as cur:
            worker = self._worker(cur, token)
            cur.execute("""
                UPDATE helper_orders SET status=CASE WHEN %s IS NOT NULL THEN 'error' ELSE 'stopped' END,
                  error=%s,password_cipher=NULL,updated_at=now(),lease_until=NULL
                WHERE worker_id=%s AND id=%s AND status='running' RETURNING user_id
            """, (error, error, worker, order_id))
            if cur.fetchone():
                if error:
                    cur.execute("INSERT INTO helper_alerts(order_id,text) VALUES (%s,%s)",
                                (order_id, f"⚠️ Заказ #{order_id}. {error}"))
                cur.execute("UPDATE helper_lots SET purchase='uncertain' WHERE order_id=%s AND purchase='pending'",
                            (order_id,))

    def reap(self):
        with self.cursor() as cur:
            cur.execute("""
              WITH expired AS (
                UPDATE helper_orders SET status='error', password_cipher=NULL, lease_until=NULL,
                   error='Связь с VPS потеряна. Проверь незавершённые покупки в игре.', updated_at=now()
                 WHERE status='running' AND lease_until <= now() RETURNING id, error
              )
              INSERT INTO helper_alerts(order_id,text) SELECT id, 'Заказ #' || id || '. ' || error FROM expired
            """)
            cur.execute("""
              UPDATE helper_lots l SET purchase='uncertain' FROM helper_orders o
              WHERE l.order_id=o.id AND o.status='error' AND l.purchase='pending'
            """)

    def alerts(self):
        with self.cursor() as cur:
            cur.execute("""
                SELECT a.id,a.text,o.user_id FROM helper_alerts a JOIN helper_orders o ON o.id=a.order_id
                WHERE a.delivered_at IS NULL ORDER BY a.id LIMIT 30
            """)
            return cur.fetchall()

    def delivered(self, alert):
        with self.cursor() as cur:
            cur.execute("UPDATE helper_alerts SET delivered_at=now() WHERE id=%s", (alert,))
