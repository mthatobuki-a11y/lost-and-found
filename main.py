import base64
import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import List, Literal, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

load_dotenv()

DB_PATH = "lost_and_found.db"

app = FastAPI(title="学校向け 落とし物管理Webアプリ")


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


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


class AIAnalysisResult(BaseModel):
    category: CategoryType = Field(
        description="カテゴリ（『水筒・弁当』『文房具』『衣類・体操服』『傘・雨具』『その他』のいずれか1つ）"
    )
    public_tags: str = Field(
        description="生徒公開用の短い特徴タグ（色、一般的な形状、ブランドなど20文字以内。個人情報や記名は絶対に含めないこと）"
    )
    secret_memo: str = Field(
        description="【非公開】窓口での本人確認用メモ（記名内容、イニシャル、細かい傷・シール・キーホルダーの特徴など詳細に記載）"
    )
    has_personal_info: bool = Field(
        description="画像内に氏名、クラス、電話番号、住所、顔写真などの個人情報や記名が写り込んでいる場合はtrue、なければfalse"
    )


class AnalyzeImageRequest(BaseModel):
    image_data: str


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


class PublicItemResponse(BaseModel):
    id: int
    image_data: str
    category: str
    public_tags: Optional[str]
    found_date: str
    found_location: str


# --- Auth Dependency ---


def verify_admin(x_admin_password: str = Header(None, alias="X-Admin-Password")):
    load_dotenv(override=True)
    expected_password = os.getenv("ADMIN_PASSWORD", "0711")
    if not x_admin_password or x_admin_password != expected_password:
        raise HTTPException(status_code=401, detail="管理者パスワードが正しくありません。")
    return True


# --- Public API ---


@app.get("/api/public/items", response_model=List[PublicItemResponse])
def get_public_items(
    q: Optional[str] = Query(None, description="検索キーワード（管理番号、特徴タグ、拾得場所）"),
    category: Optional[str] = Query(None, description="カテゴリフィルタ"),
):
    conn = get_db_connection()
    cursor = conn.cursor()

    query = """
        SELECT id, image_data, category, public_tags, found_date, found_location
        FROM items
        WHERE status = '保管中'
    """
    params = []

    if category and category != "すべて":
        query += " AND category = ?"
        params.append(category)

    if q and q.strip():
        kw = f"%{q.strip()}%"
        # 管理番号、特徴タグ、拾得場所を横断部分一致検索
        clean_id = q.strip().replace("No.", "").replace("no.", "").replace("#", "")
        query += " AND (CAST(id AS TEXT) LIKE ? OR CAST(id AS TEXT) = ? OR public_tags LIKE ? OR found_location LIKE ?)"
        params.extend([kw, clean_id, kw, kw])

    query += " ORDER BY id DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


# --- Admin APIs ---


@app.get("/api/admin/verify")
def check_admin_auth(_: bool = Depends(verify_admin)):
    return {"status": "ok"}


@app.post("/api/admin/analyze-image", response_model=AIAnalysisResult)
def analyze_image(req: AnalyzeImageRequest, _: bool = Depends(verify_admin)):
    load_dotenv(override=True)
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="環境変数 GEMINI_API_KEY が設定されていません。.envファイルを確認してください。",
        )

    image_str = req.image_data
    mime_type = "image/jpeg"
    if "," in image_str:
        header, b64_data = image_str.split(",", 1)
        if "image/png" in header:
            mime_type = "image/png"
        elif "image/webp" in header:
            mime_type = "image/webp"
    else:
        b64_data = image_str

    try:
        image_bytes = base64.b64decode(b64_data)
    except Exception:
        raise HTTPException(status_code=400, detail="画像データのデコードに失敗しました。")

    prompt = (
        "この学校の落とし物の画像を解析し、指定されたJSONスキーマに従って情報を抽出してください。\n"
        "1. category: 「水筒・弁当」「文房具」「衣類・体操服」「傘・雨具」「その他」から最も適切なものを1つ選択。\n"
        "2. public_tags: 生徒向け公開カタログに表示する短い特徴タグ（色・メーカー・種類など、20文字以内）。"
        "※注意：ここには記名（氏名・イニシャル）や固有の暗証番号など個人情報は絶対に含めないでください。\n"
        "3. secret_memo: 教員が窓口で持ち主本人か確認するための非公開メモ。写っている記名、クラス・名前、キーホルダーやシールの詳細、傷の位置などを具体的に記述してください。\n"
        "4. has_personal_info: 画像内に名前・苗字・イニシャル・クラス・学年・個人を特定できる情報が写り込んでいる場合は true、なければ false にしてください。"
    )

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                prompt,
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AIAnalysisResult,
                temperature=0.1,
            ),
        )
        result_data = json.loads(response.text)
        # 20文字制限の安全調整
        if "public_tags" in result_data and result_data["public_tags"]:
            result_data["public_tags"] = result_data["public_tags"][:20]
        return result_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gemini AI解析エラー: {str(e)}")


@app.post("/api/admin/items")
def create_item(req: CreateItemRequest, _: bool = Depends(verify_admin)):
    today_dt = datetime.now()
    found_date_str = req.found_date if req.found_date else today_dt.strftime("%Y-%m-%d")
    try:
        base_dt = datetime.strptime(found_date_str, "%Y-%m-%d")
    except ValueError:
        base_dt = today_dt
        found_date_str = today_dt.strftime("%Y-%m-%d")

    expire_date_str = (base_dt + timedelta(days=90)).strftime("%Y-%m-%d")
    public_tags_trimmed = (req.public_tags or "")[:20]

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO items (
            image_data, category, public_tags, found_date, found_location,
            secret_memo, has_personal_info, status, expire_date
        ) VALUES (?, ?, ?, ?, ?, ?, ?, '保管中', ?)
        """,
        (
            req.image_data,
            req.category,
            public_tags_trimmed,
            found_date_str,
            req.found_location,
            req.secret_memo or "",
            bool(req.has_personal_info),
            expire_date_str,
        ),
    )
    new_id = cursor.lastrowid
    conn.commit()

    cursor.execute("SELECT * FROM items WHERE id = ?", (new_id,))
    created_item = dict(cursor.fetchone())
    conn.close()
    return created_item


@app.get("/api/admin/items")
def get_admin_items(_: bool = Depends(verify_admin)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM items WHERE status = '保管中' ORDER BY id DESC")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


@app.post("/api/admin/items/{item_id}/return")
def return_item(item_id: int, req: ReturnItemRequest, _: bool = Depends(verify_admin)):
    if not req.returned_to.strip() or not req.handled_by.strip():
        raise HTTPException(status_code=400, detail="返却先と対応教員名は必須です。")

    returned_at_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM items WHERE id = ?", (item_id,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="指定された落とし物が見つかりません。")

    cursor.execute(
        """
        UPDATE items
        SET status = '返却済', returned_to = ?, returned_at = ?, handled_by = ?
        WHERE id = ?
        """,
        (req.returned_to.strip(), returned_at_str, req.handled_by.strip(), item_id),
    )
    conn.commit()
    conn.close()
    return {"message": "返却処理が完了しました。", "id": item_id, "returned_at": returned_at_str}


@app.get("/api/admin/returned-history")
def get_returned_history(_: bool = Depends(verify_admin)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM items WHERE status = '返却済' ORDER BY returned_at DESC, id DESC")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


@app.get("/api/admin/expired-items")
def get_expired_items(_: bool = Depends(verify_admin)):
    today_str = datetime.now().strftime("%Y-%m-%d")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM items
        WHERE status = '保管中' AND expire_date < ?
        ORDER BY expire_date ASC, id ASC
        """,
        (today_str,),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


@app.post("/api/admin/batch-dispose")
def batch_dispose(req: BatchDisposeRequest, _: bool = Depends(verify_admin)):
    if not req.item_ids:
        raise HTTPException(status_code=400, detail="処分対象のアイテムが選択されていません。")

    conn = get_db_connection()
    cursor = conn.cursor()
    placeholders = ",".join(["?"] * len(req.item_ids))
    disposed_at_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    handled_by_val = req.handled_by.strip() if req.handled_by else "一括処分"

    cursor.execute(
        f"""
        UPDATE items
        SET status = '処分済', returned_at = ?, handled_by = ?
        WHERE id IN ({placeholders}) AND status = '保管中'
        """,
        [disposed_at_str, handled_by_val] + req.item_ids,
    )
    updated_count = cursor.rowcount
    conn.commit()
    conn.close()
    return {"message": f"{updated_count}件のアイテムを処分済みに更新しました。", "disposed_count": updated_count}


from fastapi.responses import FileResponse

# 静的ファイルの配信（staticフォルダにあってもルート直下にあっても自動対応）
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
