import base64
import csv
import io
import json
import os
import sqlite3
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

app = FastAPI(title="学校向け 落とし物管理Webアプリ")

# --- データベース接続（PostgreSQLとSQLiteの自動判別ハイブリッド） ---
DATABASE_URL = os.getenv("DATABASE_URL")

USE_POSTGRES = False
if DATABASE_URL:
    USE_POSTGRES = True
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        USE_POSTGRES = False

DB_PATH = "lost_and_found.db"


class DBClient:
    def __init__(self):
        self.is_pg = USE_POSTGRES

    def get_conn(self):
        if self.is_pg:
            # RenderのPostgresURLは postgres:// から始まることがあるので postgresql:// に補正
            url = DATABASE_URL
            if url.startswith("postgres://"):
                url = url.replace("postgres://", "postgresql://", 1)
            conn = psycopg2.connect(url)
            return conn
        else:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            return conn

    def execute(self, query: str, params: tuple = (), fetch: Literal["all", "one", "none"] = "none", return_last_id: bool = False):
        conn = self.get_conn()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if self.is_pg else conn.cursor()
        
        # SQLiteの ? プレースホルダを PostgreSQL用 %s に置換
        if self.is_pg:
            # CAST(id AS TEXT) はそのままで両対応可能
            pg_query = query.replace("?", "%s")
            if return_last_id:
                pg_query = pg_query.rstrip(" ;") + " RETURNING id;"
            cursor.execute(pg_query, params)
        else:
            cursor.execute(query, params)

        data = None
        last_id = None
        if return_last_id:
            if self.is_pg:
                res = cursor.fetchone()
                last_id = res["id"] if isinstance(res, dict) else res[0]
            else:
                last_id = cursor.lastrowid

        if fetch == "all":
            rows = cursor.fetchall()
            if self.is_pg:
                data = [dict(r) for r in rows]
            else:
                data = [dict(r) for r in rows]
        elif fetch == "one":
            row = cursor.fetchone()
            if row:
                data = dict(row)

        conn.commit()
        cursor.close()
        conn.close()

        if return_last_id:
            return last_id
        return data


db = DBClient()


def init_db():
    conn = db.get_conn()
    cursor = conn.cursor()
    
    # itemsテーブル
    if db.is_pg:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id SERIAL PRIMARY KEY,
                term_name TEXT DEFAULT '現学期',
                term_item_number INTEGER,
                image_data TEXT NOT NULL,
                category TEXT NOT NULL,
                public_tags TEXT,
                found_date TEXT NOT NULL,
                found_location TEXT NOT NULL,
                secret_memo TEXT,
                has_personal_info BOOLEAN DEFAULT FALSE,
                status TEXT DEFAULT '保管中',
                expire_date TEXT NOT NULL,
                returned_to TEXT,
                returned_at TEXT,
                handled_by TEXT
            );
            """
        )
    else:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                term_name TEXT DEFAULT '現学期',
                term_item_number INTEGER,
                image_data TEXT NOT NULL,
                category TEXT NOT NULL,
                public_tags TEXT,
                found_date TEXT NOT NULL,
                found_location TEXT NOT NULL,
                secret_memo TEXT,
                has_personal_info BOOLEAN DEFAULT FALSE,
                status TEXT DEFAULT '保管中',
                expire_date TEXT NOT NULL,
                returned_to TEXT,
                returned_at TEXT,
                handled_by TEXT
            );
            """
        )
    
    # 旧カラムが存在しない場合のマイグレーション（SQLite用）
    try:
        cursor.execute("SELECT term_item_number FROM items LIMIT 1")
    except Exception:
        conn.rollback() if db.is_pg else None
        cursor.execute("ALTER TABLE items ADD COLUMN term_item_number INTEGER;")
        cursor.execute("ALTER TABLE items ADD COLUMN term_name TEXT DEFAULT '現学期';")
        conn.commit()

    # 既存データで term_item_number が NULL のものを id と同期
    cursor.execute("UPDATE items SET term_item_number = id WHERE term_item_number IS NULL;")
    conn.commit()
    cursor.close()
    conn.close()


init_db()


# --- 現在の学期を取得するヘルパー ---
def get_current_term_name(dt: Optional[datetime] = None) -> str:
    if dt is None:
        dt = datetime.now()
    # 日本の学校暦（4月〜7月: 1学期, 8月〜12月: 2学期, 1月〜3月: 3学期）
    year = dt.year
    month = dt.month
    if 4 <= month <= 7:
        return f"{year}年度 1学期"
    elif 8 <= month <= 12:
        return f"{year}年度 2学期"
    else:
        school_year = year - 1
        return f"{school_year}年度 3学期"


# --- Schemas ---

CategoryType = Literal[
    "タブレット",
    "スマホ",
    "イヤホン",
    "水筒・弁当",
    "教科書",
    "文房具",
    "衣類・体操服",
    "傘・雨具",
    "その他",
]


class CreateItemRequest(BaseModel):
    image_data: str
    category: str
    public_tags: Optional[str] = ""
    found_date: Optional[str] = None
    found_location: str
    secret_memo: Optional[str] = ""
    has_personal_info: bool = False


class ReturnItemRequest(BaseModel):
    returned_to: str
    handled_by: str


class BatchDisposeRequest(BaseModel):
    item_ids: List[int]
    handled_by: Optional[str] = ""


class ResetTermRequest(BaseModel):
    new_term_name: str


class PublicItemResponse(BaseModel):
    id: int
    term_item_number: int
    term_name: Optional[str]
    image_data: str
    category: str
    public_tags: Optional[str]
    found_date: str
    found_location: str


# --- Public API ---


@app.get("/api/public/items", response_model=List[PublicItemResponse])
def get_public_items(
    q: Optional[str] = Query(None, description="検索キーワード（管理番号、特徴タグ、拾得場所）"),
    category: Optional[str] = Query(None, description="カテゴリフィルタ"),
):
    query = """
        SELECT id, COALESCE(term_item_number, id) as term_item_number, term_name, image_data, category, public_tags, found_date, found_location
        FROM items
        WHERE status = '保管中'
    """
    params = []

    if category and category != "すべて":
        query += " AND category = ?"
        params.append(category)

    if q and q.strip():
        kw = f"%{q.strip()}%"
        clean_id = q.strip().replace("No.", "").replace("no.", "").replace("#", "")
        query += " AND (CAST(term_item_number AS TEXT) LIKE ? OR CAST(id AS TEXT) = ? OR CAST(term_item_number AS TEXT) = ? OR public_tags LIKE ? OR found_location LIKE ?)"
        params.extend([kw, clean_id, clean_id, kw, kw])

    query += " ORDER BY id DESC"
    rows = db.execute(query, tuple(params), fetch="all")
    return rows


# --- Admin APIs ---


@app.get("/api/admin/system-info")
def get_system_info():
    current_term = get_current_term_name()
    # 現学期に登録された最新の term_item_number を取得
    row = db.execute(
        "SELECT MAX(term_item_number) as max_num FROM items WHERE term_name = ?",
        (current_term,),
        fetch="one",
    )
    max_num = row.get("max_num") if row and row.get("max_num") is not None else 0

    # 過去学期の一覧
    terms = db.execute(
        "SELECT DISTINCT term_name FROM items WHERE term_name IS NOT NULL ORDER BY term_name DESC",
        fetch="all",
    )
    return {
        "current_term": current_term,
        "next_item_number": max_num + 1,
        "database_type": "PostgreSQL (クラウド永久保存)" if db.is_pg else "SQLite (ローカル)",
        "all_terms": [t["term_name"] for t in terms],
    }


@app.post("/api/admin/items")
def create_item(req: CreateItemRequest):
    today_dt = datetime.now()
    found_date_str = req.found_date if req.found_date else today_dt.strftime("%Y-%m-%d")
    try:
        base_dt = datetime.strptime(found_date_str, "%Y-%m-%d")
    except ValueError:
        base_dt = today_dt
        found_date_str = today_dt.strftime("%Y-%m-%d")

    expire_date_str = (base_dt + timedelta(days=90)).strftime("%Y-%m-%d")
    public_tags_trimmed = (req.public_tags or "")[:20]

    # 現在の学期名
    current_term = get_current_term_name(base_dt)

    # 学期内での次の連番を計算 (1, 2, 3...)
    row = db.execute(
        "SELECT MAX(term_item_number) as max_num FROM items WHERE term_name = ?",
        (current_term,),
        fetch="one",
    )
    current_max = row.get("max_num") if row and row.get("max_num") is not None else 0
    next_term_num = current_max + 1

    new_id = db.execute(
        """
        INSERT INTO items (
            term_name, term_item_number,
            image_data, category, public_tags, found_date, found_location,
            secret_memo, has_personal_info, status, expire_date
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '保管中', ?)
        """,
        (
            current_term,
            next_term_num,
            req.image_data,
            req.category,
            public_tags_trimmed,
            found_date_str,
            req.found_location,
            req.secret_memo or "",
            bool(req.has_personal_info),
            expire_date_str,
        ),
        return_last_id=True,
    )

    created_item = db.execute("SELECT * FROM items WHERE id = ?", (new_id,), fetch="one")
    return created_item


@app.get("/api/admin/items")
def get_admin_items():
    rows = db.execute("SELECT * FROM items WHERE status = '保管中' ORDER BY id DESC", fetch="all")
    return rows


@app.post("/api/admin/items/{item_id}/return")
def return_item(item_id: int, req: ReturnItemRequest):
    if not req.returned_to.strip() or not req.handled_by.strip():
        raise HTTPException(status_code=400, detail="返却先と対応教員名は必須です。")

    returned_at_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    item = db.execute("SELECT id FROM items WHERE id = ?", (item_id,), fetch="one")
    if not item:
        raise HTTPException(status_code=404, detail="指定された落とし物が見つかりません。")

    db.execute(
        """
        UPDATE items
        SET status = '返却済', returned_to = ?, returned_at = ?, handled_by = ?
        WHERE id = ?
        """,
        (req.returned_to.strip(), returned_at_str, req.handled_by.strip(), item_id),
    )
    return {"message": "返却処理が完了しました。", "id": item_id, "returned_at": returned_at_str}


@app.get("/api/admin/returned-history")
def get_returned_history(term: Optional[str] = Query(None)):
    if term and term != "すべて":
        rows = db.execute(
            "SELECT * FROM items WHERE status = '返却済' AND term_name = ? ORDER BY returned_at DESC, id DESC",
            (term,),
            fetch="all",
        )
    else:
        rows = db.execute("SELECT * FROM items WHERE status = '返却済' ORDER BY returned_at DESC, id DESC", fetch="all")
    return rows


@app.get("/api/admin/expired-items")
def get_expired_items():
    today_str = datetime.now().strftime("%Y-%m-%d")
    rows = db.execute(
        """
        SELECT * FROM items
        WHERE status = '保管中' AND expire_date < ?
        ORDER BY expire_date ASC, id ASC
        """,
        (today_str,),
        fetch="all",
    )
    return rows


@app.post("/api/admin/batch-dispose")
def batch_dispose(req: BatchDisposeRequest):
    if not req.item_ids:
        raise HTTPException(status_code=400, detail="処分対象のアイテムが選択されていません。")

    disposed_at_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    handled_by_val = req.handled_by.strip() if req.handled_by else "一括処分"

    placeholders = ",".join(["?"] * len(req.item_ids))
    db.execute(
        f"""
        UPDATE items
        SET status = '処分済', returned_at = ?, handled_by = ?
        WHERE id IN ({placeholders}) AND status = '保管中'
        """,
        tuple([disposed_at_str, handled_by_val] + req.item_ids),
    )
    return {"message": f"{len(req.item_ids)}件のアイテムを処分済みに更新しました。"}


# --- 過去学期台帳のCSVエクスポート（過去の記録をまとめて保存・出力） ---
@app.get("/api/admin/export-csv")
def export_csv(term: Optional[str] = Query(None)):
    query = "SELECT * FROM items"
    params = ()
    if term and term != "すべて":
        query += " WHERE term_name = ?"
        params = (term,)
    query += " ORDER BY id ASC"

    rows = db.execute(query, params, fetch="all")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "通しID", "学期", "学期内管理番号", "品目カテゴリ", "特徴タグ", "拾得日",
        "拾得場所", "ステータス", "本人確認メモ", "個人情報検知",
        "保管期限日", "返却先", "返却日時", "対応教員"
    ])

    for r in rows:
        writer.writerow([
            r.get("id"),
            r.get("term_name") or "",
            f"No.{r.get('term_item_number') or r.get('id')}",
            r.get("category"),
            r.get("public_tags") or "",
            r.get("found_date"),
            r.get("found_location"),
            r.get("status"),
            r.get("secret_memo") or "",
            "あり" if r.get("has_personal_info") else "なし",
            r.get("expire_date"),
            r.get("returned_to") or "",
            r.get("returned_at") or "",
            r.get("handled_by") or "",
        ])

    csv_data = output.getvalue().encode("utf-8-sig")  # Excelで文字化けしないUTF-8 BOM付き
    filename = f"lost_and_found_{term or 'all'}.csv"
    quoted_filename = urllib.parse.quote(filename)

    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted_filename}"},
    )


# --- 静的ファイルの配信 ---
@app.get("/")
def serve_index():
    if os.path.exists("static/index.html"):
        return FileResponse("static/index.html")
    return FileResponse("index.html")


@app.get("/admin.html")
def serve_admin():
    if os.path.exists("static/admin.html"):
        return FileResponse("static/admin.html")
    return FileResponse("admin.html")


if os.path.exists("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
