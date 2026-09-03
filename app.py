from flask import Flask, jsonify, request, render_template, session, redirect, url_for
from urllib.parse import urlparse, urljoin, urlencode
from functools import wraps
import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

# app.py はプロジェクト直下に置く。
# 実体（templates / static / data）は bousai_app/ 配下にあるので、そこを参照する。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, 'bousai_app')

app = Flask(
    __name__,
    template_folder=os.path.join(APP_DIR, 'templates'),
    static_folder=os.path.join(APP_DIR, 'static'),
)
app.secret_key = 'your-secret-key-here'

# 管理者認証情報
ADMIN_CREDENTIALS = {
    'admin': '123'
}

# ────────────────────────────────
# 気象警報・注意報設定
PREFECTURE_CODE = "020000"  # 青森県
AREA_NAME = "青森市"
AREA_CODE = "0220100"  # 青森市（市町村コード）

WARNING_URL = (
    f"https://www.jma.go.jp/bosai/warning/data/r8/{PREFECTURE_CODE}.json"
)

JST = timezone(timedelta(hours=9))

# 警報・注意報のコード一覧
WARNING_CODES = {
    "00": "解除",
    "02": "暴風雪警報",
    "03": "レベル3大雨警報",
    "04": "洪水警報",
    "05": "暴風警報",
    "06": "大雪警報",
    "07": "波浪警報",
    "08": "レベル3高潮警報",
    "09": "レベル3土砂災害警報",
    "10": "レベル2大雨注意報",
    "12": "大雪注意報",
    "13": "風雪注意報",
    "14": "雷注意報",
    "15": "強風注意報",
    "16": "波浪注意報",
    "17": "融雪注意報",
    "18": "洪水注意報",
    "19": "レベル2高潮注意報",
    "20": "濃霧注意報",
    "21": "乾燥注意報",
    "22": "なだれ注意報",
    "23": "低温注意報",
    "24": "霜注意報",
    "25": "着氷注意報",
    "26": "着雪注意報",
    "27": "その他の注意報",
    "29": "レベル2土砂災害注意報",
    "32": "暴風雪特別警報",
    "33": "レベル5大雨特別警報",
    "35": "暴風特別警報",
    "36": "大雪特別警報",
    "37": "波浪特別警報",
    "38": "レベル5高潮特別警報",
    "39": "レベル5土砂災害特別警報",
    "43": "レベル4大雨危険警報",
    "48": "レベル4高潮危険警報",
    "49": "レベル4土砂災害危険警報"
}

# ────────────────────────────────
# サンプルデータの読み込み
DATA_FILE = os.path.join(APP_DIR, 'data', 'shelters.json')
INSTRUCTIONS_FILE = os.path.join(APP_DIR, 'data', 'instructions.json')
GROUPS_FILE = os.path.join(APP_DIR, 'data', 'groups.json')

def load_json(path, default):
    """JSONファイルを読み込む（存在しない・壊れている場合は default を返す）"""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

shelters = load_json(DATA_FILE, [])
instructions = load_json(INSTRUCTIONS_FILE, [])
groups = load_json(GROUPS_FILE, [])

def save_instructions():
    """指示ボードのデータをファイルに保存する"""
    try:
        with open(INSTRUCTIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(instructions, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def save_shelters():
    """避難所データをファイルに保存する"""
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(shelters, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def save_groups():
    """グループデータをファイルに保存する"""
    try:
        with open(GROUPS_FILE, 'w', encoding='utf-8') as f:
            json.dump(groups, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
# ────────────────────────────────

# ────────────────────────────────
# 認証関連の設定とヘルパー関数
def is_safe_url(target):
    """リダイレクト先URLが安全かどうかチェック"""
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

def login_required(f):
    """認証が必要なページに付けるデコレータ"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            # 現在のURLをnextパラメータとしてログイン画面にリダイレクト
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def get_japan_time():
    """日本時間（JST）の現在時刻を取得する"""
    return datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")


def geocode_address(address):
    """Nominatimで住所を一度だけ検索し、成功時だけ座標を返す"""
    params = urlencode({'q': address, 'format': 'jsonv2', 'limit': 1})
    request = urllib.request.Request(
        f'https://nominatim.openstreetmap.org/search?{params}',
        headers={'User-Agent': 'bousai-app-shelter-registration/1.0'},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            results = json.loads(response.read())
        if not results:
            return None
        latitude = float(results[0]['lat'])
        longitude = float(results[0]['lon'])
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            return None
        return latitude, longitude
    except (KeyError, TypeError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return None


def ensure_shelter_locations(items):
    """位置情報がない既存避難所を名称で一度だけジオコーディングする"""
    changed = False
    for shelter in items:
        name = str(shelter.get('name', '')).strip()
        latitude = shelter.get('latitude')
        longitude = shelter.get('longitude')
        if shelter.get('location_geocoded'):
            continue
        if latitude is not None and longitude is not None and shelter.get('address') and shelter.get('address') != name:
            continue
        if not name:
            continue
        coordinates = geocode_address(f'{name}, 日本')
        if coordinates is None:
            continue
        shelter['address'] = shelter.get('address') or name
        shelter['latitude'], shelter['longitude'] = coordinates
        shelter['location_geocoded'] = True
        changed = True
    if changed:
        save_shelters()
    return items


def fetch_walking_route(start_latitude, start_longitude, end_latitude, end_longitude):
    """徒歩プロファイルのルーティングAPIから道路ルートを取得する"""
    coordinates = f'{start_longitude},{start_latitude};{end_longitude},{end_latitude}'
    params = urlencode({'overview': 'full', 'geometries': 'geojson', 'steps': 'false'})
    route_url = f'https://routing.openstreetmap.de/routed-foot/route/v1/foot/{coordinates}?{params}'
    request = urllib.request.Request(
        route_url,
        headers={'User-Agent': 'bousai-app-walking-route/1.0'},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read())
        route = data.get('routes', [])[0]
        geometry = route.get('geometry', {}).get('coordinates', [])
        if not geometry:
            return None
        return {
            'geometry': geometry,
            'distance': route.get('distance'),
            'duration': route.get('duration'),
        }
    except (IndexError, KeyError, TypeError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return None


def get_current_user_groups():
    """現在のログインユーザーが所属するグループだけを返す"""
    username = session.get('group_member_name') or session.get('username')
    if not username:
        return []
    return [
        group for group in groups
        if any(member.get('name') == username for member in group.get('members', []))
    ]


def format_report_time(iso_str):
    """気象庁の発表時刻（ISO形式）をJSTの表示用文字列に変換する"""
    if not iso_str:
        return "不明"
    try:
        parsed = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        if parsed.tzinfo:
            parsed = parsed.astimezone(JST)
        return parsed.strftime("%Y年%m月%d日 %H:%M")
    except ValueError:
        return iso_str


def filter_shelters(district=None):
    """district 指定があれば一致する避難所のみ、なければ全件を返す"""
    return [s for s in shelters if not district or s.get('district') == district]


def search_shelters(query=''):
    """避難所名または地区名で避難所を検索する"""
    query = query.strip().lower()
    if not query:
        return list(shelters)
    return [
        shelter for shelter in shelters
        if query in str(shelter.get('name', '')).lower()
        or query in str(shelter.get('district', '')).lower()
    ]


def get_shelter_statuses():
    """登録済みの避難所状況を発信ボード用の項目に変換する"""
    statuses = []
    for shelter in shelters:
        has_status = shelter.get('status_updated_at') or any(
            shelter.get(field) not in (None, '')
            for field in ('capacity', 'supply_status', 'status_note')
        )
        if not has_status:
            continue

        statuses.append({
            'type': 'shelter_status',
            'shelter_id': shelter.get('id'),
            'shelter': shelter.get('name', ''),
            'capacity': shelter.get('capacity'),
            'supply_status': shelter.get('supply_status', ''),
            'status_note': shelter.get('status_note', ''),
            'updated_at': shelter.get('status_updated_at', ''),
            'district': shelter.get('district', ''),
        })
    return statuses


def get_board_items(selected_district=''):
    """住民向け指示と避難所状況を発信ボードの表示順で返す"""
    resident_instructions = [
        dict(item, type='instruction')
        for item in instructions
        if item.get('target') == '住民'
        and (not selected_district or item.get('district') == selected_district)
    ]
    shelter_statuses = [
        item for item in get_shelter_statuses()
        if not selected_district or not item.get('district')
        or item.get('district') == selected_district
    ]
    return resident_instructions + shelter_statuses


@app.context_processor
def inject_board_items():
    """ログイン不要の共通発信欄へ住民向け情報を渡す"""
    return {
        'base_board_items': get_board_items(),
        'base_board_districts': sorted({
            str(item.get('district') or '全域')
            for item in get_board_items()
            if item.get('district')
        }),
        'shelters': shelters,
    }


def parse_area_warnings(warning_data):
    """気象庁の新形式JSONから対象市区町村の発表・継続中の情報を抽出する"""
    if not isinstance(warning_data, list):
        raise ValueError("気象庁の警報・注意報データが新形式の配列ではありません")

    warnings = []
    seen_codes = set()
    report_datetimes = []

    for report in warning_data:
        if not isinstance(report, dict):
            continue

        report_datetime = report.get("reportDatetime")
        if isinstance(report_datetime, str) and report_datetime:
            report_datetimes.append(report_datetime)

        warning = report.get("warning")
        if not isinstance(warning, dict):
            continue

        class20_items = warning.get("class20Items", [])
        if not isinstance(class20_items, list):
            continue

        area = next(
            (
                item for item in class20_items
                if isinstance(item, dict)
                and item.get("areaCode") == AREA_CODE
            ),
            None
        )
        if not area:
            continue

        kinds = area.get("kinds", [])
        if not isinstance(kinds, list):
            continue

        for kind in kinds:
            if not isinstance(kind, dict):
                continue

            status = kind.get("status", "")
            code = kind.get("code", "")
            if status not in ("発表", "継続") or not code or code in seen_codes:
                continue

            warnings.append({
                "name": WARNING_CODES.get(
                    code,
                    f"不明な警報・注意報 (コード: {code})"
                ),
                "code": code,
                "status": status
            })
            seen_codes.add(code)

    latest_report_datetime = max(report_datetimes, default="")
    return warnings, latest_report_datetime


def get_weather_warnings():
    """対象市区町村の警報・注意報を取得する"""
    try:
        # 青森県の新形式（令和8年～）警報・注意報データを取得
        with urllib.request.urlopen(url=WARNING_URL, timeout=10) as res:
            warning_data = json.loads(res.read())

        warnings, report_datetime = parse_area_warnings(warning_data)

        return {
            "area_name": AREA_NAME,
            "warnings": warnings,
            "report_time": format_report_time(report_datetime),
            "last_fetch_time": get_japan_time()
        }

    except Exception:
        return {
            "area_name": AREA_NAME,
            "warnings": [],
            "report_time": "取得失敗",
            "last_fetch_time": get_japan_time(),
            "error": True
        }


# トップページ：templates/index.html を返す（住民向け指示も表示する）
@app.route('/')
def index():
    resident_notices = [i for i in instructions if i.get('target') == '住民']
    return render_template(
        'index.html',
        resident_notices=resident_notices,
        user_groups=get_current_user_groups(),
    )

# ログインページ
@app.route('/login', methods=['GET', 'POST'])
def login():
    # リダイレクト先を取得（デフォルトは避難所登録画面）
    next_url = request.args.get('next') or request.form.get('next')

    # 安全でないURLの場合はデフォルトページにリダイレクト
    if not next_url or not is_safe_url(next_url):
        next_url = url_for('shelter_register')

    if request.method == 'POST':
        password = request.form.get('password', '').strip()

        # 認証チェック
        username = next(
            (name for name, registered_password in ADMIN_CREDENTIALS.items()
             if registered_password == password),
            None
        )
        if username:
            session['logged_in'] = True
            session['username'] = username
            # ログイン成功後は指定されたページにリダイレクト
            return redirect(next_url)
        return render_template('login.html', error=True, message="パスワードが正しくありません。", next=next_url)

    # ログイン済みの場合は指定されたページにリダイレクト
    if session.get('logged_in'):
        return redirect(next_url)

    return render_template('login.html', next=next_url)


@app.route('/group_create', methods=['GET', 'POST'])
@login_required
def group_create():
    if request.method == 'POST':
        group_name = request.form.get('group_name', '').strip()
        invite_code = request.form.get('invite_code', '').strip().upper()

        if not group_name:
            return render_template('group_create.html', error='グループ名を入力してください')
        if not invite_code:
            return render_template('group_create.html', error='招待コードを入力してください')
        if any(group.get('invite_code', '').upper() == invite_code for group in groups):
            return render_template(
                'group_create.html',
                error='その招待コードは既に使用されています',
                group_name=group_name,
                invite_code=invite_code,
            )

        groups.append({
            'id': max((group.get('id', 0) for group in groups), default=0) + 1,
            'group_name': group_name,
            'invite_code': invite_code,
            'members': [{
                'name': session.get('username', ''),
                'status': '未確認',
            }],
        })
        session['group_member_name'] = session.get('username', '')
        save_groups()
        return redirect(url_for('index'))

    return render_template('group_create.html')


@app.route('/group_join', methods=['GET', 'POST'])
@login_required
def group_join():
    if request.method == 'POST':
        group_name = request.form.get('group_name', '').strip()
        invite_code = request.form.get('invite_code', '').strip().upper()
        member_name = request.form.get('username', '').strip()

        if not group_name:
            return render_template('group_join.html', error='グループ名を入力してください')
        if not invite_code:
            return render_template('group_join.html', error='招待コードを入力してください', group_name=group_name)

        group = next(
            (item for item in groups if item.get('invite_code', '').upper() == invite_code),
            None,
        )
        if group is None:
            return render_template(
                'group_join.html',
                error='存在しない招待コードです。',
                group_name=group_name,
                invite_code=invite_code,
            )
        if group.get('group_name') != group_name:
            return render_template(
                'group_join.html',
                error='グループ名と招待コードが一致しません',
                group_name=group_name,
                invite_code=invite_code,
            )
        if not member_name:
            return render_template(
                'group_join.html',
                error='名前を入力してください',
                group_name=group_name,
                invite_code=invite_code,
            )

        if not any(member.get('name') == member_name for member in group.setdefault('members', [])):
            group['members'].append({'name': member_name, 'status': '未確認'})
            save_groups()
        session['group_member_name'] = member_name
        return redirect(url_for('index'))

    return render_template('group_join.html')


@app.route('/group_safety_update', methods=['POST'])
@login_required
def group_safety_update():
    shelter_name = request.form.get('shelter_name', '').strip()
    shelter = next((item for item in shelters if item.get('name') == shelter_name), None)
    if shelter is not None:
        member_name = session.get('group_member_name') or session.get('username')
        for group in get_current_user_groups():
            for member in group.get('members', []):
                if member.get('name') == member_name:
                    member['status'] = f'{shelter_name}に避難済み'
        save_groups()
    return redirect(url_for('index'))

# ログアウト
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# 避難所登録ページ
@app.route('/shelter_register', methods=['GET', 'POST'])
@login_required
def shelter_register():
    if request.method == 'POST':
        form_type = request.form.get('form_type', 'shelter')
        if form_type == 'crowd':
            shelter_name = request.form.get('crowd_shelter_name', '').strip()
            crowd_status_value = request.form.get('crowd_status', '').strip()
            if not shelter_name:
                return render_template('shelter_register.html', crowd_error='避難所名を選択してください。', shelters=shelters)
            if not crowd_status_value:
                return render_template('shelter_register.html', crowd_error='現在の避難者数を入力してください。', shelters=shelters)
            try:
                crowd_status = int(crowd_status_value)
            except ValueError:
                crowd_status = -1
            if crowd_status < 0:
                return render_template('shelter_register.html', crowd_error='現在の避難者数は0以上の整数で入力してください。', shelters=shelters)

            shelter = next((item for item in shelters if str(item.get('name', '')).strip() == shelter_name), None)
            if shelter is None:
                return render_template('shelter_register.html', crowd_error='選択した避難所が見つかりません。', shelters=shelters)
            shelter['crowd_status'] = crowd_status
            shelter['current_evacuees'] = crowd_status
            save_shelters()
            return render_template('shelter_register.html', shelters=shelters, crowd_success='現在の避難者数が登録されました。')

        shelter_name = request.form.get('name', '').strip()
        address = request.form.get('address', '').strip()
        capacity_value = request.form.get('capacity', '').strip()
        if not shelter_name:
            return render_template('shelter_register.html', error=True, message='避難所名を入力してください', shelters=shelters)
        if not address:
            return render_template('shelter_register.html', error=True, message='住所を入力してください', shelter_name=shelter_name, address=address, capacity=capacity_value, shelters=shelters)
        try:
            capacity = int(capacity_value)
        except ValueError:
            capacity = -1
        if capacity < 0:
            return render_template('shelter_register.html', error=True, message='最大収容人数は0以上の整数で入力してください', shelter_name=shelter_name, address=address, capacity=capacity_value, shelters=shelters)

        coordinates = geocode_address(address)
        if coordinates is None:
            return render_template('shelter_register.html', error=True, message='住所の位置情報を取得できませんでした。住所を確認して再度お試しください。', shelter_name=shelter_name, address=address, capacity=capacity_value, shelters=shelters)

        next_id = max((s.get('id', 0) for s in shelters), default=0) + 1
        shelters.append({
            'id': next_id,
            'name': shelter_name,
            'address': address,
            'latitude': coordinates[0],
            'longitude': coordinates[1],
            'capacity': capacity,
            'pet_allowed': request.form.get('pet_allowed', 'no'),
            'barrier_free': request.form.get('barrier_free', 'no'),
            'current_evacuees': 0,
            'crowd_status': 0,
        })
        save_shelters()
        return render_template('shelter_register.html', shelters=shelters, success=True, message='避難所を登録しました。')

    return render_template('shelter_register.html', shelters=shelters)

# 避難所検索ページ
@app.route('/shelter_search')
def shelter_search():
    return render_template('shelter_search.html')

# 全施設一覧ページ
@app.route('/all_shelters')
def all_shelters():
    query = request.args.get('query', '').strip()
    return render_template('search_results.html', results=ensure_shelter_locations(search_shelters(query)), query=query)


# 指示ボード：市民はログインなしで閲覧でき、管理者のみ登録フォームを表示する
@app.route('/board', methods=['GET', 'POST'])
def board():
    selected_district = request.args.get('district', '').strip()
    selected_importance = request.args.get('importance', '').strip()
    selected_confirmation = request.args.get('confirmation', '').strip()
    is_admin = bool(session.get('logged_in'))

    def board_items():
        items = get_board_items(selected_district)
        if selected_importance:
            items = [item for item in items if item.get('type') == 'shelter_status' or item.get('importance', '情報') == selected_importance]
        if selected_confirmation == 'unconfirmed':
            items = [item for item in items if not item.get('confirmed')]
        return items

    if request.method == 'POST':
        if not is_admin:
            return redirect(url_for('login', next=request.url))

        action = request.form.get('action', '')
        item_id = request.form.get('item-id', '').strip()

        if action in ('edit_instruction', 'delete_instruction'):
            try:
                instruction_id = int(item_id)
            except ValueError:
                instruction_id = -1
            instruction = next(
                (item for item in instructions if item.get('id') == instruction_id
                 and item.get('target') == '住民'),
                None
            )
            if instruction is None:
                message = '対象の指示が見つかりません。'
            elif action == 'delete_instruction':
                instructions.remove(instruction)
                save_instructions()
                message = '指示を削除しました。'
            else:
                instruction_content = request.form.get('instruction-content', '').strip()
                importance = request.form.get('importance', '').strip()
                district = request.form.get('instruction-district', '').strip()
                if not instruction_content or not importance or not district:
                    message = '指示内容・重要度・対象地区を入力してください。'
                else:
                    instruction.update({
                        'content': instruction_content,
                        'importance': importance,
                        'district': district,
                        'shelter': request.form.get('instruction-shelter', '').strip(),
                        'time': request.form.get('instruction-time', '').strip() or instruction.get('time', ''),
                        'updated_at': get_japan_time(),
                    })
                    save_instructions()
                    message = '指示を更新しました。'
            return render_template(
                'board.html',
                instructions=board_items(),
                districts=sorted({str(i.get('district') or '全域') for i in instructions if i.get('target') == '住民'}),
                district_filter=selected_district,
                importance_filter=selected_importance,
                confirmation_filter=selected_confirmation,
                is_admin=is_admin,
                success_message=message if instruction is not None and action == 'delete_instruction' or instruction is not None and action == 'edit_instruction' and '入力してください' not in message else None,
                error_message=message if instruction is None or '入力してください' in message else None,
            )

        if action in ('edit_shelter_status', 'delete_shelter_status'):
            try:
                shelter_id = int(item_id)
            except ValueError:
                shelter_id = -1
            shelter = next((item for item in shelters if item.get('id') == shelter_id), None)
            if shelter is None:
                message = '対象の避難所状況が見つかりません。'
            elif action == 'delete_shelter_status':
                for field in ('capacity', 'supply_status', 'status_note', 'status_updated_at'):
                    shelter.pop(field, None)
                save_shelters()
                message = '避難所状況を削除しました。'
            else:
                capacity = request.form.get('capacity', '').strip()
                try:
                    shelter['capacity'] = int(capacity) if capacity else None
                except ValueError:
                    shelter['capacity'] = None
                shelter['supply_status'] = request.form.get('supply-status', '').strip()
                shelter['status_note'] = request.form.get('status-note', '').strip()
                shelter['status_updated_at'] = get_japan_time()
                save_shelters()
                message = '避難所状況を更新しました。'
            return render_template(
                'board.html',
                instructions=board_items(),
                districts=sorted({str(i.get('district') or '全域') for i in instructions if i.get('target') == '住民'}),
                district_filter=selected_district,
                importance_filter=selected_importance,
                confirmation_filter=selected_confirmation,
                is_admin=is_admin,
                success_message=message if shelter is not None else None,
                error_message=message if shelter is None else None,
            )

        if 'instruction-content' in request.form:
            instruction_content = request.form.get('instruction-content', '').strip()
            importance = request.form.get('importance', '').strip()
            instruction_time = request.form.get('instruction-time', '').strip()
            district = request.form.get('instruction-district', '').strip()
            shelter_name = request.form.get('instruction-shelter', '').strip()

            if not instruction_content or not importance or not district:
                return render_template(
                    'board.html',
                    instructions=board_items(),
                    districts=sorted({str(i.get('district') or '全域') for i in instructions if i.get('target') == '住民'}),
                    district_filter=selected_district,
                    importance_filter=selected_importance,
                    confirmation_filter=selected_confirmation,
                    is_admin=is_admin,
                    error_message='指示内容・重要度・対象地区を入力してください。'
                )

            new_instruction = {
                'id': max((i.get('id', 0) for i in instructions), default=0) + 1,
                'target': '住民',
                'content': instruction_content,
                'importance': importance,
                'district': district,
                'shelter': shelter_name,
                'time': instruction_time or datetime.now(JST).strftime('%H:%M'),
                'created_at': get_japan_time(),
                'updated_at': get_japan_time(),
            }
            instructions.append(new_instruction)
            try:
                with open(INSTRUCTIONS_FILE, 'w', encoding='utf-8') as f:
                    json.dump(instructions, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

            return render_template(
                'board.html',
                instructions=board_items(),
                districts=sorted({str(i.get('district') or '全域') for i in instructions if i.get('target') == '住民'}),
                district_filter=selected_district,
                importance_filter=selected_importance,
                confirmation_filter=selected_confirmation,
                is_admin=is_admin,
                success_message='指示を登録しました。'
            )

        if 'shelter-name' in request.form:
            shelter_name = request.form.get('shelter-name', '').strip()
            capacity = request.form.get('capacity', '').strip()
            supply_status = request.form.get('supply-status', '').strip()
            status_note = request.form.get('status-note', '').strip()

            if not shelter_name:
                return render_template(
                    'board.html',
                    instructions=board_items(),
                    districts=sorted({str(i.get('district') or '全域') for i in instructions if i.get('target') == '住民'}),
                    district_filter=selected_district,
                    importance_filter=selected_importance,
                    confirmation_filter=selected_confirmation,
                    is_admin=is_admin,
                    error_message='避難所名を入力してください。'
                )

            matched = False
            for shelter in shelters:
                if str(shelter.get('name', '')).strip() == shelter_name:
                    shelter['capacity'] = int(capacity) if capacity else shelter.get('capacity')
                    shelter['supply_status'] = supply_status or shelter.get('supply_status', '')
                    shelter['status_note'] = status_note or shelter.get('status_note', '')
                    shelter['status_updated_at'] = get_japan_time()
                    matched = True
                    break

            if not matched:
                shelters.append({
                    'id': max((s.get('id', 0) for s in shelters), default=0) + 1,
                    'name': shelter_name,
                    'capacity': int(capacity) if capacity else 0,
                    'supply_status': supply_status,
                    'status_note': status_note,
                    'status_updated_at': get_japan_time(),
                })

            try:
                with open(DATA_FILE, 'w', encoding='utf-8') as f:
                    json.dump(shelters, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

            return render_template(
                'board.html',
                instructions=board_items(),
                districts=sorted({str(i.get('district') or '全域') for i in instructions if i.get('target') == '住民'}),
                district_filter=selected_district,
                importance_filter=selected_importance,
                confirmation_filter=selected_confirmation,
                is_admin=is_admin,
                success_message='避難所状況を登録しました。'
            )

    districts = sorted({str(i.get('district') or '全域') for i in instructions if i.get('target') == '住民'})
    return render_template(
        'board.html',
        instructions=board_items(),
        districts=districts,
        district_filter=selected_district,
        importance_filter=selected_importance,
        confirmation_filter=selected_confirmation,
        is_admin=is_admin
    )

# 検索結果ページ：templates/search_results.html を返す
@app.route('/search_results')
def search_results():
    query = request.args.get('query', '').strip()
    return render_template('search_results.html', results=ensure_shelter_locations(search_shelters(query)), query=query)

# JSON API：/shelters?district=地区名
@app.route('/shelters', methods=['GET'])
def get_shelters():
    results = filter_shelters(request.args.get('district'))

    if not results:
        # 見つからなければエラー JSON を返す
        return jsonify({'error': 'No shelters found'}), 404

    # 見つかったらリストを JSON で返す
    return jsonify(results)


@app.route('/api/walking_route')
def api_walking_route():
    """現在地から避難所までの徒歩ルートをJSONで返す"""
    try:
        start_latitude = float(request.args['start_latitude'])
        start_longitude = float(request.args['start_longitude'])
        end_latitude = float(request.args['end_latitude'])
        end_longitude = float(request.args['end_longitude'])
        coordinates = (start_latitude, start_longitude, end_latitude, end_longitude)
        if not all((-90 <= coordinates[index] <= 90 if index % 2 == 0 else -180 <= coordinates[index] <= 180)
                   for index in range(4)):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return jsonify({'error': '有効な座標が必要です'}), 400

    route = fetch_walking_route(
        start_latitude, start_longitude, end_latitude, end_longitude
    )
    if route is None:
        return jsonify({'error': '徒歩ルートを取得できませんでした'}), 502
    return jsonify(route)

# 気象警報・注意報API
@app.route('/api/weather_warnings')
def api_weather_warnings():
    """気象警報・注意報をJSON形式で返すAPI"""
    return jsonify(get_weather_warnings())

if __name__ == '__main__':
    app.run(debug=True, port=5000)
