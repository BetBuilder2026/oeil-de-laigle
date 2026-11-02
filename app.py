# ============================================================
# ŒIL DE L'AIGLE — Application d'analyse sportive
# ============================================================

import copy
import hashlib
import html
import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone

import google.generativeai as genai
import gspread
import requests
import streamlit as st
from google.oauth2.service_account import Credentials

# Compatibilité avec streamlit-cookies-manager.
if not hasattr(st, "cache"):
    def _legacy_cache_compat(func=None, **kwargs):
        for name in (
            "allow_output_mutation",
            "hash_funcs",
            "suppress_st_warning",
        ):
            kwargs.pop(name, None)
        if func is not None:
            return st.cache_resource(func)
        return st.cache_resource(**kwargs)

    st.cache = _legacy_cache_compat

from streamlit_cookies_manager import EncryptedCookieManager


# ============================================================
# PAGE ET PRÉSENTATION
# ============================================================

st.set_page_config(
    page_title="Œil de l'Aigle",
    page_icon="🦅",
    layout="centered",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
html, body, [class*="css"] {
    font-family: 'Segoe UI', Arial, sans-serif !important;
}
[data-testid="stIconMaterial"],
.material-icons,
.material-icons-outlined,
.material-icons-round,
.material-icons-sharp,
.material-symbols-outlined,
.material-symbols-rounded,
.material-symbols-sharp,
span[class*="material-symbols"],
i[class*="material"] {
    font-family: 'Material Symbols Rounded', 'Material Icons' !important;
    font-size: inherit !important;
}
.stButton>button {
    border-radius: 10px;
    font-weight: 600;
    padding: 0.55em 1.3em;
}
.big-title {
    font-size: 2.2em;
    font-weight: 800;
    margin-bottom: 0.1em;
}
.subtitle {
    font-size: 1.08em;
    color: #999;
    margin-bottom: 1.2em;
    line-height: 1.5em;
}
.badge-manual, .badge-verified {
    padding: 4px 12px;
    border-radius: 14px;
    font-size: 0.88em;
    font-weight: 700;
    display: inline-block;
}
.badge-manual {
    background-color: #000000;
    color: #ffffff;
}
.badge-verified {
    background-color: #d4edda;
    color: #155724;
}
.option-card {
    border: 1px solid #3a3a3c;
    border-radius: 12px;
    padding: 0.9em 1.2em;
    margin-bottom: 0.7em;
    background-color: #1c1c1e;
}
.option-card, .option-card * {
    color: #f2f2f2 !important;
}
.step-title {
    font-size: 1.4em;
    font-weight: 800;
    margin-top: 0.3em;
    margin-bottom: 0.5em;
}
</style>
""",
    unsafe_allow_html=True,
)


# ============================================================
# CONSTANTES
# ============================================================

TITLE = "Œil de l’Aigle"

AVERAGE_ODDS = 1.5
MAX_POOL_SIMPLE_COMBO = 10
MAX_POOL_MULTI_AUTO = 15
MAX_MANUAL_GLOBAL = 20

# Règle de cote minimale par sélection
MIN_ODDS_SIMPLE = 1.50
MIN_ODDS_COMBO = 1.47

# Marge par défaut autour de la cote cible
DEFAULT_TOLERANCE = 0.10

SCHEMA_VERSION = 2

SPORT_LIST = [
    "Football",
    "Basketball",
    "Tennis",
    "Hockey sur glace",
    "Football américain",
    "Tout",
]

SPORT_ICONS = {
    "Football": "⚽",
    "Basketball": "🏀",
    "Basketball Euroleague": "🏀",
    "Tennis": "🎾",
    "Hockey sur glace": "🏒",
    "Football américain": "🏈",
    "Football americain": "🏈",
    "Tout": "🎯",
}

FOOTBALL_COMPETITIONS = {
    "Toutes les compétitions": "ALL",
    "Premier League": "PL",
    "Ligue 1": "FL1",
    "Liga": "PD",
    "Serie A": "SA",
    "Bundesliga": "BL1",
    "Ligue des Champions": "CL",
    "Championship": "ELC",
    "Eredivisie": "DED",
    "Primeira Liga": "PPL",
    "Brasileiro": "BSA",
}

# Niveaux de confiance attribués par l’application après analyse
CONFIDENCE_RANK = {
    "Excellent": 3,
    "Bon": 2,
    "Moyen": 1,
    "Faible": 0,
}

ANALYSIS_DEFAULTS = {
    "schema_version": SCHEMA_VERSION,
    "page": "analysis",
    "step": "setup",
    "sport": "Football",
    "competition": "ALL",
    "cote_cible": 2.0,
    "analysis_id": None,
    "data_sport": "Football",
    "has_match_data": False,
    "matches_pool": [],
    "relevant_matches": [],
    "data_info_text": "",
    "data_cotes_text": "",
    "data_extra_text": "",
    "options_results": {},
    "extra_results": {},
    "final_result": None,
    "final_justification": "",
    "final_justification_combo": None,
    "mode": None,
    "coupon_max_size": None,
    "jours_fenetre": None,
    "pool_limit": None,
    "tolerance": DEFAULT_TOLERANCE,
    "manual_nb_matches": 3,
    "manual_draft": [],
}

SNAPSHOT_KEYS = [key for key in ANALYSIS_DEFAULTS if key != "page"]

STAGE_LABELS = {
    "setup": "Paramétrage initial",
    "manual_entry": "Saisie manuelle des matchs",
    "inforequest": "Informations complémentaires",
    "options_ready": "Options et cotes",
    "final": "Analyse finalisée",
}

# ============================================================
# UTILITAIRES
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def esc(value):
    return html.escape(str(value))


def fingerprint(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_probability(value):
    if isinstance(value, bool):
        return 0.0
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    if value > 1:
        value = value / 100.0
    return max(0.0, min(1.0, value))


def valid_odds(value):
    try:
        value = float(value)
        return math.isfinite(value) and value > 1.0
    except (TypeError, ValueError):
        return False


def parse_odd(value):
    try:
        value = float(str(value).replace(",", "."))
        return value if math.isfinite(value) and value > 1.0 else None
    except (TypeError, ValueError):
        return None


def min_odds_for(coupon_size):
    if coupon_size <= 1:
        return MIN_ODDS_SIMPLE
    return MIN_ODDS_COMBO


def estimated_selections(cote_cible):
    return max(
        1,
        math.ceil(
            math.log(float(cote_cible)) / math.log(AVERAGE_ODDS)
        ),
    )


def determine_mode_params(cote_cible):
    estimate = estimated_selections(cote_cible)

    if cote_cible <= 3.0:
        mode = "simple_double"
        max_size = 2
        days = 2
        cap = MAX_POOL_SIMPLE_COMBO
    elif estimate < 5:
        mode = "combo_info"
        max_size = estimate
        days = 7
        cap = MAX_POOL_SIMPLE_COMBO
    else:
        mode = "multi"
        max_size = None
        days = 7
        cap = MAX_POOL_MULTI_AUTO

    count = estimated_selections(cote_cible)
    reserve = math.ceil(count / 0.6)
    recommended = min(cap, max(2, reserve))

    return mode, max_size, days, recommended, cap


def fix_elisions(text):
    text = str(text or "")
    text = text.replace(" ’", "’").replace("‘ ", "‘")

    patterns = [
        (r"([LlDdJjCcNnSs])s+([aàeéèiîoôuùh])", r"\u0001’\u0002"),
        (r"([LlDdJjCcNnSs])’ ([aàeéèiîoôuùh])", r"\u0001’\u0002"),
        (r"c [eé]", "c’est"),
        (r"j [aà]", "j’ai"),
        (r"n [eé]", "n’"),
        (r"s [eé]", "s’"),
    ]

    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)

    return text


def clean_justification_text(text):
    text = re.sub(r"[*_#`]", "", str(text or ""))
    text = re.sub(r"[ ]{2,}", " ", text)
    return fix_elisions(text).strip()


def display_aerated(text):
    for paragraph in str(text or "").splitlines():
        paragraph = paragraph.strip()
        if paragraph:
            st.write(paragraph)


def extract_info_by_mid(text):
    result = {}
    current = None

    invisible = (
        chr(8206)
        + chr(8207)
        + chr(8234)
        + chr(8235)
        + chr(8236)
        + chr(8237)
    )

    pattern = (
        "^[" + " " + chr(9) + invisible + "]*"
        + "(M[0-9]+)"
        + "[" + " " + chr(9) + invisible + "]*:"
    )

    for line in (text or "").splitlines():
        match = re.match(pattern, line)
        if match:
            current = match.group(1).upper()
            result[current] = ""
        elif current:
            result[current] = result[current] + line + chr(10)

    return {
        mid: content.strip()
        for mid, content in result.items()
    }


def missing_information(text, matches):
    parsed = extract_info_by_mid(text)
    return [
        match["mid"]
        for match in matches
        if not parsed.get(match["mid"], "").strip()
    ]


def parse_cotes_input(text):
    result = {}
    errors = []

    for number, line in enumerate((text or "").splitlines(), 1):
        if not line.strip():
            continue

        match = re.match(
            "^[Mm]([0-9]+)[ ]*:[ ]*(.+)$",
            line.strip(),
        )

        if not match:
            errors.append(
                f"Ligne {number} : format M1: A=1.50 attendu."
            )
            continue

        mid = "M" + match.group(1)
        values = {}
        tokens = match.group(2).split()

        for token in tokens:
            token_match = re.match(
                "^([A-Za-z]+)=([0-9]+[.,][0-9]+|[0-9]+)$",
                token,
            )

            if not token_match:
                errors.append(
                    f"Ligne {number} : option invalide « {token} ». "
                    "Format attendu : A=1.50"
                )
                continue

            letter = token_match.group(1).upper()
            odd = parse_odd(token_match.group(2))

            if odd is None:
                errors.append(
                    f"Ligne {number} : cote invalide pour {letter}."
                )
                continue

            if letter in values:
                errors.append(
                    f"Ligne {number} : option {letter} répétée."
                )
                continue

            values[letter] = odd

        if values:
            result[mid] = values

    return result, errors


def parse_extra_input(text):
    result = {}
    errors = []

    for number, line in enumerate((text or "").splitlines(), 1):
        if not line.strip():
            continue

        match = re.match(
            "^[Mm]([0-9]+)[_]EXTRA[ ]*:[ ]*(.+)$",
            line.strip(),
        )

        if not match:
            errors.append(
                f"EXTRA ligne {number} : format "
                "M1_EXTRA: Total_Under_225.5=1.66 attendu."
            )
            continue

        mid = "M" + match.group(1)
        payload = match.group(2)

        if "=" not in payload:
            errors.append(
                f"EXTRA ligne {number} : cote manquante. "
                "Format attendu : M1_EXTRA: Total_Under_225.5=1.66"
            )
            continue

        description, raw_odd = payload.rpartition("=")[0], payload.rpartition("=")[2]
        description = description.strip()
        odd = parse_odd(raw_odd)

        if not description:
            errors.append(
                f"EXTRA ligne {number} : description du marché manquante."
            )
            continue

        if odd is None:
            errors.append(
                f"EXTRA ligne {number} : cote invalide."
            )
            continue

        result.setdefault(mid, []).append(
            {
                "description": description,
                "cote": odd,
            }
        )

    return result, errors


def clear_widget_state():
    for key in list(S.keys()):
        if key.startswith("widget_"):
            del S[key]


def initialize_analysis():
    for key, value in ANALYSIS_DEFAULTS.items():
        if key not in S:
            S[key] = copy.deepcopy(value)


def reset_analysis():
    clear_widget_state()

    for key, value in ANALYSIS_DEFAULTS.items():
        S[key] = copy.deepcopy(value)

    S["confirm_delete"] = None
    S["last_saved_signature"] = None
    S["history_error"] = ""


def reset_results():
    S["options_results"] = {}
    S["extra_results"] = {}
    S["final_result"] = None
    S["final_justification"] = ""
    S["final_justification_combo"] = None


def widget_value(name, initial):
    key = f"widget_{name}"
    if key not in S:
        S[key] = copy.deepcopy(initial)
    return key


def capture_text(field, widget_key):
    value = S.get(widget_key, "")

    if S.get(field) != value:
        S[field] = value
        S.pop("widget_confirm_markets", None)

        if field == "data_info_text":
            reset_results()
        else:
            S["final_result"] = None
            S["final_justification"] = ""
            S["final_justification_combo"] = None


# ============================================================
# GOOGLE SHEETS ET COMPTES
# ============================================================

@st.cache_resource
def get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=scopes,
    )
    return gspread.authorize(credentials)


def get_spreadsheet():
    return get_gspread_client().open_by_url(
        st.secrets["GOOGLE_SHEET_URL"]
    )


def get_users_sheet():
    return get_spreadsheet().worksheet("Users")


def get_history_sheet():
    return get_spreadsheet().worksheet("History")


def hash_password(password):
    # Compatibilité avec les comptes déjà enregistrés.
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def find_user_row(email):
    normalized = email.strip().lower()
    records = get_users_sheet().get_all_values()

    for index, row in enumerate(records[1:], start=2):
        if row and row[0].strip().lower() == normalized:
            return index, row

    return None, None


def create_user(email, password):
    get_users_sheet().append_row(
        [
            email.strip().lower(),
            hash_password(password),
            utc_now().isoformat(),
            "",
            "",
        ],
        value_input_option="RAW",
    )


def authenticate_user(email, password):
    _, row = find_user_row(email)
    return bool(
        row
        and len(row) > 1
        and row[1] == hash_password(password)
    )


def set_session_token(email, token):
    row_number, _ = find_user_row(email)
    if row_number:
        get_users_sheet().update_cell(row_number, 5, token)


def check_session_token(token):
    if not token:
        return None
    for row in get_users_sheet().get_all_values()[1:]:
        if len(row) > 4 and row[4] == token:
            return row[0]
    return None


def clear_session_token(email):
    set_session_token(email, "")


# ============================================================
# SAUVEGARDE ET REPRISE
# ============================================================

def snapshot_session_data():
    return {
        key: copy.deepcopy(S[key])
        for key in SNAPSHOT_KEYS
        if key in S
    }


def history_signature(data):
    return fingerprint(data)


def record_progress(stage=None, notify=False):
    if not S.get("user_email"):
        return False

    if not S.get("analysis_id"):
        S["analysis_id"] = str(uuid.uuid4())

    if stage is not None:
        S["step"] = stage

    data = snapshot_session_data()
    signature = history_signature(data)

    if signature == S.get("last_saved_signature"):
        if notify:
            st.info("Cette version est déjà sauvegardée.")
        return True

    try:
        payload = json.dumps(data, ensure_ascii=False)
        if len(payload) > 45000:
            raise ValueError(
                "L'analyse est trop volumineuse pour une seule cellule "
                "Google Sheets. Réduisez les textes complémentaires."
            )

        get_history_sheet().append_row(
            [
                S["user_email"].strip().lower(),
                utc_now().isoformat(),
                S["step"],
                S["sport"],
                str(S["cote_cible"]),
                payload,
            ],
            value_input_option="RAW",
        )
        S["last_saved_signature"] = signature
        S["history_error"] = ""
        if notify:
            st.success("Analyse sauvegardée.")
        return True
    except Exception as error:
        S["history_error"] = str(error)[:350]
        if notify:
            st.error(
                "Sauvegarde impossible : " + S["history_error"]
            )
        return False


def get_history_entries(email):
    entries = []
    normalized = email.strip().lower()

    for row_number, row in enumerate(
        get_history_sheet().get_all_values()[1:],
        start=2,
    ):
        if len(row) < 6 or row[0].strip().lower() != normalized:
            continue
        try:
            data = json.loads(row[5]) if row[5] else {}
            if not isinstance(data, dict):
                data = {}
        except (ValueError, TypeError):
            data = {}

        entries.append({
            "row_number": row_number,
            "email": row[0],
            "timestamp": row[1],
            "stage": row[2],
            "sport": row[3],
            "cote_cible": row[4],
            "session_data": data,
        })
    return entries


def parse_timestamp(value):
    try:
        dt = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def period_label(timestamp):
    dt = parse_timestamp(timestamp)
    if dt.year == 1:
        return "📁 Date inconnue"
    days = (utc_now().date() - dt.date()).days
    if days <= 0:
        return "🟢 Aujourd'hui"
    if days == 1:
        return "🟡 Hier"
    if days <= 7:
        return "🔵 Cette semaine"
    if days <= 30:
        return "🟣 Ce mois-ci"
    return "⚪ Plus ancien"


def group_entries_by_analysis(entries):
    groups = {}
    for entry in entries:
        aid = (
            entry["session_data"].get("analysis_id")
            or f"row_{entry['row_number']}"
        )
        groups.setdefault(aid, []).append(entry)

    result = []
    for aid, rows in groups.items():
        rows.sort(
            key=lambda row: (
                parse_timestamp(row["timestamp"]),
                row["row_number"],
            )
        )
        result.append({
            "analysis_id": aid,
            "latest": rows[-1],
            "row_numbers": [
                row["row_number"] for row in rows
            ],
        })
    return sorted(
        result,
        key=lambda group: parse_timestamp(
            group["latest"]["timestamp"]
        ),
        reverse=True,
    )


def delete_history_rows(row_numbers):
    owned = {
        entry["row_number"]
        for entry in get_history_entries(S["user_email"])
    }
    requested = set(row_numbers)
    if not requested.issubset(owned):
        raise ValueError(
            "L'historique a changé. Actualisez avant de supprimer."
        )
    sheet = get_history_sheet()
    for row_number in sorted(requested, reverse=True):
        sheet.delete_rows(row_number)


def infer_resume_step(data, old_stage):
    if (data.get("final_result") or {}).get("combo"):
        return "final"

    explicit = data.get("step")
    if explicit in STAGE_LABELS:
        return explicit

    if data.get("options_results"):
        return "options_ready"

    if data.get("matches_pool"):
        if old_stage == "info_request":
            return "options_ready"
        return "info_request"

    if old_stage == "manual_entry" or data.get("manual_draft"):
        return "manual_entry"

    return "setup"


def resume_analysis(entry):
    if (
        entry["email"].strip().lower()
        != S["user_email"].strip().lower()
    ):
        raise ValueError(
            "Cette analyse appartient à un autre compte."
        )

    data = entry["session_data"]
    if not data:
        raise ValueError(
            "La sauvegarde ne contient pas de données lisibles."
        )

    safe_data = copy.deepcopy(data)
    if not isinstance(safe_data.get("final_result"), dict):
        safe_data["final_result"] = {}

    step = infer_resume_step(safe_data, entry["stage"])
    reset_analysis()

    for key in SNAPSHOT_KEYS:
        if key in data:
            S[key] = copy.deepcopy(data[key])

    try:
        S["cote_cible"] = float(
            data.get("cote_cible")
            or entry.get("cote_cible")
            or 2.0
        )
    except (TypeError, ValueError):
        S["cote_cible"] = 2.0

    S["schema_version"] = SCHEMA_VERSION
    S["analysis_id"] = (
        data.get("analysis_id") or str(uuid.uuid4())
    )
    S["sport"] = (
        data.get("sport") or entry["sport"] or "Football"
    )
    S["data_sport"] = S["sport"]
    S["threshold"] = validity_rate(S["cote_cible"])
    S["is_auto_mode"] = (
        S["sport"] == "Football"
        or (
            S["sport"] == "Basketball"
            and S.get("competition") == "NBA"
        )
    )

    pool = S.get("matches_pool") or []
    S["has_match_data"] = bool(pool)

    if pool and not S.get("relevant_matches"):
        S["relevant_matches"] = copy.deepcopy(pool)
        if step != "final":
            step = "info_request"

    if not S.get("manual_draft") and pool:
        S["manual_draft"] = copy.deepcopy(pool)

    if "manual_nb_matches" not in data and pool:
        S["manual_nb_matches"] = len(pool)

    if step in ("info_request", "options_ready") and not pool:
        step = "setup"

    if (
        step == "options_ready"
        and missing_information(
            S["data_info_text"],
            S["relevant_matches"],
        )
    ):
        step = "info_request"

    S["page"] = "analysis"
    S["step"] = step
    S["last_saved_signature"] = history_signature(
        snapshot_session_data()
    )
    S["flash_message"] = (
        "Analyse restaurée. Les anciennes cotes et informations "
        "peuvent avoir changé : vérifiez-les avant de recalculer."
    )


# ============================================================
# API SPORTIVES
# ============================================================

def fetch_football_matches(competition_code, days):
    matches = []
    errors = []
    codes = (
        [
            code
            for code in FOOTBALL_COMPETITIONS.values()
            if code != "ALL"
        ]
        if competition_code == "ALL"
        else [competition_code]
    )
    params = {
        "dateFrom": utc_now().date().isoformat(),
        "dateTo": (
            utc_now() + timedelta(days=days)
        ).date().isoformat(),
        "status": "SCHEDULED",
    }
    headers = {
        "X-Auth-Token": st.secrets["FOOTBALL_API_KEY"]
    }

    for code in codes:
        try:
            response = requests.get(
                "https://api.football-data.org/v4/"
                f"competitions/{code}/matches",
                headers=headers,
                params=params,
                timeout=20,
            )
            response.raise_for_status()
            for match in response.json().get("matches", []):
                matches.append({
                    "external_id": str(match.get("id", "")),
                    "sport": "Football",
                    "home": match["homeTeam"]["name"],
                    "away": match["awayTeam"]["name"],
                    "date": match["utcDate"],
                    "competition": (
                        match["competition"]["name"]
                    ),
                })
        except Exception as error:
            errors.append(
                f"{code} : {str(error)[:140]}"
            )

    return matches, errors


def fetch_nba_matches(days):
    matches = []
    errors = []
    params = {
        "start_date": utc_now().date().isoformat(),
        "end_date": (
            utc_now() + timedelta(days=days)
        ).date().isoformat(),
        "per_page": 100,
    }
    headers = {
        "Authorization": st.secrets["BASKETBALL_API_KEY"]
    }

    try:
        for _ in range(5):
            response = requests.get(
                "https://api.balldontlie.io/v1/games",
                headers=headers,
                params=params,
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()

            for game in payload.get("data", []):
                status = str(game.get("status", ""))
                if int(game.get("period") or 0) > 0:
                    continue
                if "final" in status.lower():
                    continue

                date = (
                    game.get("datetime")
                    or game.get("date", "")
                )
                parsed = parse_timestamp(date)
                if (
                    len(str(date)) > 10
                    and parsed.year > 1
                    and parsed <= utc_now()
                ):
                    continue

                matches.append({
                    "external_id": str(game.get("id", "")),
                    "sport": "Basketball",
                    "home": game["home_team"]["full_name"],
                    "away": game["visitor_team"]["full_name"],
                    "date": date,
                    "competition": "NBA",
                })

            cursor = (
                (payload.get("meta") or {}).get("next_cursor")
            )
            if not cursor:
                break
            params["cursor"] = cursor
    except Exception as error:
        errors.append("NBA : " + str(error)[:180])

    return matches, errors


def prepare_pool(matches, limit):
    unique = {}
    for match in matches:
        key = (
            match.get("sport"),
            match.get("external_id")
            or (
                match.get("home"),
                match.get("away"),
                match.get("date"),
            ),
        )
        unique[key] = match

    ordered = sorted(
        unique.values(),
        key=lambda match: (
            parse_timestamp(match.get("date")),
            match.get("home", ""),
        ),
    )
    result = []
    for index, match in enumerate(ordered[:limit], 1):
        item = dict(match)
        item["mid"] = f"M{index}"
        item["status"] = "en_attente"
        result.append(item)
    return result


# ============================================================
# GÉNÉRATION GEMINI
# ============================================================

def configure_gemini():
    genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])
    model_name = st.secrets.get(
        "GEMINI_MODEL",
        "gemini-flash-lite-latest",
    )
    return genai.GenerativeModel(model_name)


def extract_json(text):
    text = str(text or "").strip()
    text = text.replace("```json", "")
    text = text.replace("```", "")
    text = text.strip()

    try:
        value = json.loads(text)
    except ValueError:
        start = text.find("{")
        if start < 0:
            raise ValueError("Aucun objet JSON dans la réponse.")
        value, _ = json.JSONDecoder().raw_decode(text[start:])

    if not isinstance(value, dict):
        raise ValueError("La réponse JSON doit être un objet.")

    return value


def generate_json(prompt):
    model = configure_gemini()
    errors = []

    for strict in (True, False):
        try:
            kwargs = {}
            if strict:
                kwargs["generation_config"] = (
                    genai.types.GenerationConfig(
                        response_mime_type="application/json"
                    )
                )
            response = model.generate_content(prompt, kwargs)
            return extract_json(response.text)
        except Exception as error:
            errors.append(str(error)[:200])

    raise ValueError(" | ".join(errors))


def normalize_confidence(value):
    value = str(value or "").strip().lower()

    if value in ("excellent", "extrême", "extreme"):
        return "Excellent"
    if value in ("bon", "bonne", "élevé", "eleve", "high"):
        return "Bon"
    if value in ("moyen", "medium"):
        return "Moyen"
    return "Faible"


def generate_match_options(match, sport, user_info_text):
    prompt = f"""
{ANTI_HALLUCINATION_RULE}
{SIMPLE_MARKET_RULES}

Match : {json.dumps(match, ensure_ascii=False)}
Sport : {sport}
Informations de ce match : {user_info_text}

Tu es l’analyste final de l’application. Ta mission est d’analyser
minutieusement toutes les informations fournies et d’identifier les
scénarios les plus fiables du match.

Consignes :
- Analyse l’ensemble du texte fourni : contexte, enjeu, forme,
domicile/extérieur, confrontations directes, statistiques, joueurs
disponibles, absences, conditions et marchés contextuels.
- Identifie entre 3 et 5 scénarios distincts et défendables.
- Explore tous les types de marchés pertinents : vainqueur, double
chance, total, handicap, les deux équipes marquent, statistiques
individuelles, ou tout autre marché justifié par les données.
- Ne te limite pas aux scénarios évidents : cherche le scénario le
plus fiable, même s’il s’agit d’un total, d’un handicap ou d’un
marché secondaire.
- Chaque scénario doit être fondé uniquement sur les informations
fournies. N’invente aucun fait, joueur, statistique ou disponibilité.
- Un joueur cité comme absent ne doit jamais apparaître dans un
scénario.
- Si un marché est ambigu ou non évaluable, ne le propose pas.

Pour chaque scénario, attribue un niveau de confiance :
- "Excellent" : convergence forte des données, peu d’incertitudes ;
- "Bon" : plusieurs données concordantes ;
- "Moyen" : éléments favorables mais incertitudes importantes ;
- "Faible" : données insuffisantes ou contradictions.

Le niveau de confiance n’est pas une probabilité calibrée et ne doit
jamais être présenté comme un pourcentage de réussite.

Réponds uniquement en JSON valide :
{{
  "synthese": "2 à 3 phrases en français",
  "candidats": [
    {{
      "marche": "intitulé du marché",
      "resultat": "résultat exact visé",
      "ligne": "ligne numérique si pertinente, sinon chaîne vide",
      "periode": "match entier",
      "niveau_confiance": "Excellent, Bon, Moyen ou Faible",
      "probabilite": 0.0,
      "justification": "explication courte fondée sur les informations"
    }}
  ]
}}

La probabilité est facultative : utilise une estimation entre 0 et 1
uniquement lorsque tu peux la défendre. Le niveau de confiance reste
le critère principal.
"""

    pool = {}
    diagnostics = []
    synthesis = ""

    for attempt in range(2):
        try:
            parsed = generate_json(prompt)
            synthesis = synthesis or fix_elisions(
                parsed.get("synthese", "")
            )

            candidats = parsed.get("candidats", [])
            if not isinstance(candidats, list):
                candidats = []

            for raw in candidats:
                if not isinstance(raw, dict):
                    continue

                marche = str(raw.get("marche", "")).strip()
                resultat = str(raw.get("resultat", "")).strip()

                if not marche or not resultat:
                    diagnostics.append(
                        "Scénario incomplet ignoré."
                    )
                    continue

                confidence = normalize_confidence(
                    raw.get("niveau_confiance", "")
                )

                option = {
                    "marche": fix_elisions(marche),
                    "resultat": fix_elisions(resultat),
                    "ligne": str(raw.get("ligne", "") or "").strip(),
                    "periode": str(
                        raw.get("periode", "match entier")
                    ).strip(),
                    "niveau_confiance": confidence,
                    "probabilite": normalize_probability(
                        raw.get("probabilite", 0)
                    ),
                    "justification": fix_elisions(
                        str(raw.get("justification", ""))
                    ),
                }

                key = (
                    option["marche"].lower()
                    + "|"
                    + option["resultat"].lower()
                    + "|"
                    + option["ligne"].lower()
                )

                if key not in pool:
                    pool[key] = option

            if len(pool) >= 3:
                break

        except Exception as error:
            diagnostics.append(
                "Appel ou lecture de la réponse : "
                + str(error)[:250]
            )

    selected = sorted(
        pool.values(),
        key=lambda option: (
            CONFIDENCE_RANK.get(
                option.get("niveau_confiance", "Faible"),
                0,
            ),
            option.get("probabilite", 0),
        ),
        reverse=True,
    )[:5]

    result = {
        "synthese": synthesis,
        "_error": "",
        "_diagnostics": list(dict.fromkeys(diagnostics)),
    }

    for letter, option in zip(("A", "B", "C", "D", "E"), selected):
        result[letter] = option

    return result


def evaluate_extra_market(match, name, user_info_text):
    prompt = f"""
{ANTI_HALLUCINATION_RULE}

Match : {json.dumps(match, ensure_ascii=False)}
Informations de ce match : {user_info_text}

Marché EXTRA à évaluer : {name}

Analyse ce marché à partir des informations fournies.
Détermine le niveau de confiance du scénario.

Niveaux possibles :
- "Excellent" : convergence forte des données, peu d’incertitudes ;
- "Bon" : plusieurs données concordantes ;
- "Moyen" : éléments favorables mais incertitudes importantes ;
- "Faible" : données insuffisantes ou contradictions.

N’invente aucun fait, joueur, statistique ou disponibilité.
Le niveau de confiance n’est pas une probabilité calibrée.

Réponds uniquement en JSON valide :
{{
  "niveau_confiance": "Excellent, Bon, Moyen ou Faible",
  "probabilite": 0.0,
  "justification": "explication courte fondée sur les informations"
}}
"""

    try:
        parsed = generate_json(prompt)

        return {
            "niveau_confiance": normalize_confidence(
                parsed.get("niveau_confiance", "")
            ),
            "probabilite": normalize_probability(
                parsed.get("probabilite", 0)
            ),
            "justification": fix_elisions(
                str(parsed.get("justification", ""))
            ),
        }

    except Exception as error:
        return {
            "niveau_confiance": "Faible",
            "probabilite": 0.0,
            "justification": (
                "Évaluation indisponible : "
                + str(error)[:200]
            ),
        }
    

# ============================================================
# RECHERCHE DU COUPON
# ============================================================

def parse_option_key(key):
    if not isinstance(key, str) or "|" not in key:
        return None, None

    mid, letter = key.split("|", 1)
    return mid.strip().upper(), letter.strip().upper()


def option_label(letter):
    return f"Option {letter}"


def option_display(option):
    if not isinstance(option, dict):
        return ""

    parts = [
        option.get("marche", ""),
        option.get("resultat", ""),
    ]

    if option.get("ligne"):
        parts.append(str(option["ligne"]))

    return " — ".join(part for part in parts if part)


def confidence_sort_key(option):
    return CONFIDENCE_RANK.get(
        option.get("niveau_confiance", "Faible"),
        0,
    )


def candidate_sort_key(candidate):
    return (
        confidence_sort_key(candidate.get("option", {})),
        candidate.get("option", {}).get("probabilite", 0),
        candidate.get("cote", 0),
    )


def build_options_pool(mid, options_result):
    pool = []

    if not isinstance(options_result, dict):
        return pool

    for letter in ("A", "B", "C", "D", "E"):
        option = options_result.get(letter)

        if not isinstance(option, dict):
            continue

        if not option.get("marche") or not option.get("resultat"):
            continue

        pool.append(
            {
                "mid": mid,
                "lettre": letter,
                "option": option,
                "source": "principal",
            }
        )

    return pool


def collect_coupon_candidates(matches, options_results, extra_results):
    candidates = []

    for match in matches:
        mid = match.get("mid")
        options_result = options_results.get(mid, {})
        candidates.extend(build_options_pool(mid, options_result))

        for extra in extra_results.get(mid, []):
            if not extra.get("description"):
                continue

            candidates.append(
                {
                    "mid": mid,
                    "lettre": "EXTRA",
                    "option": {
                        "marche": extra.get("description", ""),
                        "resultat": "",
                        "ligne": "",
                        "periode": "match entier",
                        "niveau_confiance": extra.get(
                            "niveau_confiance",
                            "Faible",
                        ),
                        "probabilite": extra.get("probabilite", 0),
                        "justification": extra.get(
                            "justification",
                            "",
                        ),
                    },
                    "cote": extra.get("cote"),
                    "source": "extra",
                }
            )

    for candidate in candidates:
        if candidate.get("source") == "extra":
            continue

        letter = candidate.get("lettre")
        cotes = S.get("data_cotes_parsed", {}).get(
            candidate.get("mid"),
            {},
        )
        candidate["cote"] = cotes.get(letter)

    return candidates


def coupon_valid(selections, cote_cible, tolerance):
    if not selections:
        return False

    mids = [selection.get("mid") for selection in selections]

    if len(mids) != len(set(mids)):
        return False

    min_odds = min_odds_for(len(selections))

    for selection in selections:
        odd = selection.get("cote")

        if not valid_odds(odd) or float(odd) < min_odds:
            return False

    total = compute_final_odds(selections)
    return abs(total - float(cote_cible)) <= float(tolerance)


def compute_final_odds(selections):
    total = 1.0

    for selection in selections:
        odd = selection.get("cote")

        if valid_odds(odd):
            total *= float(odd)

    return round(total, 2)


def select_options_for_coupon(
    matches,
    options_results,
    extra_results,
    cote_cible,
    tolerance,
):
    candidates = collect_coupon_candidates(
        matches,
        options_results,
        extra_results,
    )

    by_mid = {}

    for candidate in candidates:
        mid = candidate.get("mid")

        if not mid:
            continue

        by_mid.setdefault(mid, []).append(candidate)

    if not by_mid:
        return None, "Aucune option exploitable."

    best = None
    best_key = None
    reason = "Aucune combinaison ne respecte la cote cible."

    mids = list(by_mid.keys())
    pools = [
        sorted(by_mid[mid], key=candidate_sort_key, reverse=True)
        for mid in mids
    ]

    for pool in pools:
        del pool[12:]

    for combination in itertools.product(*pools):
        selections = [
            {
                "mid": candidate["mid"],
                "lettre": candidate["lettre"],
                "option": candidate["option"],
                "cote": candidate.get("cote"),
                "source": candidate.get("source"),
            }
            for candidate in combination
        ]

        if not coupon_valid(selections, cote_cible, tolerance):
            continue

        confidence_total = sum(
            confidence_sort_key(selection["option"])
            for selection in selections
        )

        total = compute_final_odds(selections)
        distance = abs(total - float(cote_cible))

        key = (
            confidence_total,
            -distance,
            total,
        )

        if best_key is None or key > best_key:
            best = selections
            best_key = key

    if best:
        reason = "Combinaison retenue automatiquement."

    return best, reason


def build_final_coupon(
    matches,
    options_results,
    extra_results,
    cote_cible,
    tolerance,
):
    candidates = collect_coupon_candidates(
        matches,
        options_results,
        extra_results,
    )

    if not candidates:
        return None, "Aucune option exploitable.", []

    usable = [
        candidate
        for candidate in candidates
        if valid_odds(candidate.get("cote"))
    ]

    if not usable:
        return None, "Aucune cote valide disponible.", []

    prompt = f"""
{ANTI_HALLUCINATION_RULE}
{SIMPLE_MARKET_RULES}

Tu dois construire un coupon sportif à partir des options déjà
analysées par l’application. Tu ne dois jamais inventer une option,
un match, une cote ou un résultat.

Cote cible : {cote_cible}
Marge acceptée : plus ou moins {tolerance}

Règles impératives :
- Utilise uniquement les options listées ci-dessous.
- Chaque match peut apparaître au maximum une fois.
- Chaque sélection doit avoir une cote supérieure ou égale à
1.50 si le coupon contient une seule sélection, ou 1.47 s’il en
contient plusieurs.
- Le produit des cotes doit être compris entre
{round(float(cote_cible) - float(tolerance), 2)}
et
{round(float(cote_cible) + float(tolerance), 2)}.
- Ne choisis pas une sélection si sa cote est absente ou invalide.
- Privilégie les niveaux de confiance les plus élevés.
- Ne modifie jamais une cote.

Options disponibles :
{json.dumps(usable, ensure_ascii=False, default=str)}

Réponds uniquement en JSON valide :
{{
  "synthese": "explication courte en français",
  "selections": [
    {{
      "mid": "identifiant du match",
      "lettre": "A, B, C, D, E ou EXTRA",
      "justification": "raison courte du choix"
    }}
  ]
}}
"""

    errors = []

    for attempt in range(3):
        try:
            parsed = generate_json(prompt)
            selections = parsed.get("selections", [])

            if not isinstance(selections, list):
                raise ValueError("Liste de sélections absente.")

            resolved = []
            lookup = {}

            for candidate in usable:
                lookup[
                    (
                        candidate.get("mid"),
                        candidate.get("lettre"),
                    )
                ] = candidate

            for raw in selections:
                if not isinstance(raw, dict):
                    continue

                mid = str(raw.get("mid", "")).strip().upper()
                letter = str(raw.get("lettre", "")).strip().upper()
                candidate = lookup.get((mid, letter))

                if not candidate:
                    continue

                resolved.append(
                    {
                        "mid": mid,
                        "lettre": letter,
                        "option": candidate["option"],
                        "cote": candidate.get("cote"),
                        "source": candidate.get("source"),
                        "justification": fix_elisions(
                            str(raw.get("justification", ""))
                        ),
                    }
                )

            if not coupon_valid(resolved, cote_cible, tolerance):
                errors.append(
                    f"Tentative {attempt + 1} : combinaison invalide."
                )
                continue

            return (
                resolved,
                fix_elisions(str(parsed.get("synthese", ""))),
                errors,
            )

        except Exception as error:
            errors.append(
                f"Tentative {attempt + 1} : {str(error)[:200]}"
            )

    fallback, reason = select_options_for_coupon(
        matches,
        options_results,
        extra_results,
        cote_cible,
        tolerance,
    )

    if fallback:
        return fallback, reason, errors

    return None, reason, errors
               
# ============================================================
# COOKIES ET AUTHENTIFICATION
# ============================================================

cookies = EncryptedCookieManager(
    prefix="oeildelaigle_",
    password=st.secrets["COOKIE_PASSWORD"],
)
if not cookies.ready():
    st.stop()

S.setdefault("logged_in", False)
S.setdefault("user_email", "")
S.setdefault("history_error", "")
S.setdefault("last_saved_signature", None)
S.setdefault("confirm_delete", None)

if not S["logged_in"]:
    token = cookies.get("session_token")
    if token:
        try:
            email = check_session_token(token)
            if email:
                S["logged_in"] = True
                S["user_email"] = email
        except Exception as error:
            st.warning(
                "Vérification de session indisponible : "
                + str(error)[:200]
            )

if not S["logged_in"]:
    st.markdown(
        "<div class='big-title'>🦅 Œil de l'Aigle</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<div class='subtitle'>Connectez-vous pour retrouver "
        "vos analyses sportives et votre historique.</div>",
        unsafe_allow_html=True,
    )

    tab_login, tab_signup = st.tabs([
        "🔐 Se connecter",
        "🆕 Créer un compte",
    ])

    with tab_login:
        email = st.text_input(
            "Adresse e-mail",
            key="login_email",
        )
        password = st.text_input(
            "Mot de passe",
            type="password",
            key="login_password",
        )
        if st.button(
            "🔐 Se connecter",
            disabled=not (
                email.strip() and password
            ),
        ):
            try:
                if authenticate_user(email, password):
                    reset_analysis()
                    token = str(uuid.uuid4())
                    set_session_token(email, token)
                    cookies["session_token"] = token
                    cookies.save()
                    S["logged_in"] = True
                    S["user_email"] = (
                        email.strip().lower()
                    )
                    st.rerun()
                else:
                    st.error(
                        "E-mail ou mot de passe incorrect."
                    )
            except Exception as error:
                st.error(
                    "Connexion impossible : "
                    + str(error)[:250]
                )

        with st.expander("Mot de passe oublié ?"):
            st.info(
                "Cette fonctionnalité n'est pas encore disponible."
            )

    with tab_signup:
        email = st.text_input(
            "Adresse e-mail",
            key="signup_email",
        )
        password = st.text_input(
            "Mot de passe",
            type="password",
            key="signup_password",
        )
        if st.button(
            "🆕 Créer mon compte",
            disabled=not (
                email.strip() and password
            ),
        ):
            try:
                if not re.fullmatch(
                    r"[^@s]+@[^@s]+.[^@s]+", email
                ):
                    st.error(
                        "Saisissez une adresse e-mail valide."
                    )
                elif find_user_row(email)[0]:
                    st.error(
                        "Un compte existe déjà avec cet e-mail."
                    )
                else:
                    create_user(email, password)
                    st.success(
                        "Compte créé. Vous pouvez vous connecter."
                    )
            except Exception as error:
                st.error(
                    "Inscription impossible : "
                    + str(error)[:250]
                )

    st.stop()

initialize_analysis()


# ============================================================
# NAVIGATION
# ============================================================

with st.sidebar:
    st.markdown("### 🦅 Œil de l'Aigle")
    st.caption(f"Connecté : {S['user_email']}")

    if st.button(
        "🎯 Nouvelle analyse",
        use_container_width=True,
    ):
        reset_analysis()
        st.rerun()

    if st.button(
        "💾 Sauvegarder l'analyse",
        use_container_width=True,
    ):
        record_progress(notify=True)

    if st.button(
        "📜 Historique",
        use_container_width=True,
    ):
        record_progress()
        S["page"] = "history"
        S["confirm_delete"] = None
        st.rerun()

    if S["page"] == "history":
        if st.button(
            "↩️ Analyse en cours",
            use_container_width=True,
        ):
            S["page"] = "analysis"
            st.rerun()

    st.write("")
    if st.button(
        "🚪 Se déconnecter",
        use_container_width=True,
    ):
        record_progress()
        try:
            clear_session_token(S["user_email"])
        except Exception as error:
            st.error(
                "Déconnexion serveur impossible : "
                + str(error)[:180]
            )
        else:
            cookies["session_token"] = ""
            cookies.save()
            reset_analysis()
            S["logged_in"] = False
            S["user_email"] = ""
            st.rerun()

if S.get("flash_message"):
    st.info(S.pop("flash_message"))

if S.get("history_error"):
    st.warning(
        "La dernière sauvegarde a échoué : "
        + S["history_error"]
        + " Vos données restent dans la session courante."
    )


# ============================================================
# HISTORIQUE
# ============================================================

if S["page"] == "history":
    st.markdown(
        "<div class='big-title'>📜 Historique de vos analyses</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Classement en UTC. Reprenez une analyse ou consultez "
        "un résultat déjà enregistré."
    )

    try:
        entries = get_history_entries(S["user_email"])
    except Exception as error:
        st.error(
            "Chargement de l'historique impossible : "
            + str(error)[:300]
        )
        st.stop()

    if not entries:
        st.info("Aucune analyse enregistrée.")
        st.stop()

    grouped = group_entries_by_analysis(entries)

    if st.button("🗑️ Tout supprimer"):
        S["confirm_delete"] = {
            "rows": [
                entry["row_number"]
                for entry in entries
            ],
            "label": "tout votre historique",
        }

    confirmation = S.get("confirm_delete")
    if confirmation:
        st.warning(
            "Confirmer la suppression de "
            + confirmation["label"]
            + " ? Cette action est irréversible."
        )
        yes, no = st.columns(2)
        with yes:
            if st.button("✅ Confirmer la suppression"):
                try:
                    delete_history_rows(
                        confirmation["rows"]
                    )
                    S["confirm_delete"] = None
                    S["last_saved_signature"] = None
                    st.rerun()
                except Exception as error:
                    st.error(str(error)[:300])
        with no:
            if st.button("❌ Annuler"):
                S["confirm_delete"] = None
                st.rerun()

    periods = {}
    for group in grouped:
        label = period_label(
            group["latest"]["timestamp"]
        )
        periods.setdefault(label, []).append(group)

    order = [
        "🟢 Aujourd'hui",
        "🟡 Hier",
        "🔵 Cette semaine",
        "🟣 Ce mois-ci",
        "⚪ Plus ancien",
        "📁 Date inconnue",
    ]

    for period in order:
        if period not in periods:
            continue
        st.markdown(f"#### {period}")

        if st.button(
            f"🗑️ Supprimer le bloc {period}",
            key=f"delete_period_{period}",
        ):
            S["confirm_delete"] = {
                "rows": [
                    row
                    for group in periods[period]
                    for row in group["row_numbers"]
                ],
                "label": (
                    f"la période « {period} »"
                ),
            }
            st.rerun()

        for group in periods[period]:
            entry = group["latest"]
            stage = STAGE_LABELS.get(
                entry["stage"],
                entry["stage"],
            )
            st.markdown(
                "<div class='option-card'>"
                f"<b>{SPORT_ICONS.get(entry['sport'], '🏆')} "
                f"{esc(entry['sport'])}</b><br>"
                f"Cote cible : {esc(entry['cote_cible'])}<br>"
                f"Étape : {esc(stage)}<br>"
                f"<small>{esc(entry['timestamp'])}</small>"
                "</div>",
                unsafe_allow_html=True,
            )

            resume_col, delete_col = st.columns(2)
            with resume_col:
                if st.button(
                    "▶️ Reprendre",
                    key=(
                        f"resume_{group['analysis_id']}"
                    ),
                ):
                    try:
                        resume_analysis(entry)
                        st.rerun()
                    except Exception as error:
                        st.error(
                            "Reprise impossible : "
                            + str(error)[:300]
                        )

            with delete_col:
                if st.button(
                    "🗑️ Supprimer",
                    key=(
                        f"delete_{group['analysis_id']}"
                    ),
                ):
                    S["confirm_delete"] = {
                        "rows": group["row_numbers"],
                        "label": "cette analyse",
                    }
                    st.rerun()

    st.stop()


# ============================================================
# ÉTAPE 1 — PARAMÉTRAGE
# ============================================================

if S["step"] == "setup":
    st.markdown(
        "<div class='big-title'>🦅 Œil de l'Aigle</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<div class='subtitle'>Choisissez votre sport et votre "
        "cote cible. L'application prépare les rencontres à étudier, "
        "puis recherche un coupon à partir de vos informations "
        "et des cotes réelles.</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Les probabilités de Gemini sont des estimations non calibrées. "
        "Aucun coupon n'est garanti."
    )

    initial_sport = (
        S["sport"]
        if S["sport"] in SPORT_LIST
        else "Football"
    )
    sport_key = widget_value("sport", initial_sport)
    sport = st.selectbox(
        "🏆 Sport",
        SPORT_LIST,
        key=sport_key,
    )

    competition = sport
    automatic = False

    if sport == "Football":
        labels = list(FOOTBALL_COMPETITIONS)
        initial = next(
            (
                label
                for label, code
                in FOOTBALL_COMPETITIONS.items()
                if code == S["competition"]
            ),
            labels[0],
        )
        key = widget_value(
            "competition_foot", initial
        )
        label = st.selectbox(
            "⚽ Compétition",
            labels,
            key=key,
        )
        competition = FOOTBALL_COMPETITIONS[label]
        automatic = True

    elif sport == "Basketball":
        initial = (
            "Euroligue (manuel)"
            if S["competition"] == "Euroligue"
            else "NBA (automatique)"
        )
        key = widget_value(
            "basket_mode", initial
        )
        choice = st.radio(
            "🏀 Mode",
            [
                "NBA (automatique)",
                "Euroligue (manuel)",
            ],
            key=key,
        )
        automatic = choice == "NBA (automatique)"
        competition = (
            "NBA" if automatic else "Euroligue"
        )

    target_key = widget_value(
        "cote_cible",
        float(S["cote_cible"]),
    )
    target = st.number_input(
        "🎯 Cote cible",
        min_value=1.10,
        max_value=1500.0,
        step=0.05,
        format="%.2f",
        key=target_key,
    )

    tolerance_key = widget_value(
        "tolerance_pct",
        float(S["tolerance"] * 100),
    )
    tolerance_pct = st.number_input(
        "Marge autour de la cote cible (%)",
        min_value=0.0,
        max_value=30.0,
        step=1.0,
        key=tolerance_key,
    )

    S["sport"] = sport
    S["data_sport"] = sport
    S["competition"] = competition
    S["cote_cible"] = float(target)
    S["is_auto_mode"] = automatic
    S["threshold"] = validity_rate(target)
    S["tolerance"] = tolerance_pct / 100.0

    st.info(
        f"Seuil individuel : {S['threshold']:.0%}. "
        f"Cote moyenne de préparation : "
        f"{AVERAGE_ODDS:.2f}. "
        f"Environ {estimated_selections(target)} "
        "sélection(s) nécessaire(s) à cette cote moyenne."
    )

    if automatic:
        (
            mode,
            max_size,
            days,
            limit,
            threshold,
        ) = determine_mode_params(target)
        S["mode"] = mode
        S["coupon_max_size"] = max_size
        S["jours_fenetre"] = days
        S["pool_limit"] = limit

        labels = {
            "simple_double": "Simple/Double",
            "combo_info": "Combo",
            "multi": "Multi",
        }
        st.info(
            f"Mode {labels[mode]} : recherche sur "
            f"{days} jours, jusqu'à {limit} rencontres "
            "à étudier. Des informations seront demandées "
            "pour chacune."
        )
        st.caption(
            "Les rencontres sont classées par date, pas "
            "sélectionnées à ce stade pour leur probabilité "
            "de réussite."
        )

        if st.button("🔍 Lancer le scan des matchs"):
            with st.spinner(
                "Récupération des rencontres..."
            ):
                try:
                    if sport == "Football":
                        matches, errors = (
                            fetch_football_matches(
                                competition,
                                days,
                            )
                        )
                    else:
                        matches, errors = (
                            fetch_nba_matches(days)
                        )
                    pool = prepare_pool(
                        matches, limit
                    )
                except Exception as error:
                    pool = []
                    errors = [str(error)[:250]]

            for error in errors:
                st.warning(error)

            if not pool:
                st.error(
                    "Aucune rencontre exploitable trouvée. "
                    "Vérifiez la compétition, la période et "
                    "les API."
                )
            else:
                reset_results()
                S["matches_pool"] = pool
                S["relevant_matches"] = copy.deepcopy(
                    pool
                )
                S["has_match_data"] = True
                S["data_info_text"] = ""
                S["data_cotes_text"] = ""
                S["data_extra_text"] = ""
                S["manual_draft"] = []
                S["analysis_id"] = str(uuid.uuid4())
                S["step"] = "info_request"

                for name in (
                    "info_text",
                    "cotes_text",
                    "extra_text",
                ):
                    S.pop(f"widget_{name}", None)

                record_progress()
                st.rerun()

    else:
        S["mode"] = "manual"
        S["coupon_max_size"] = None
        S["jours_fenetre"] = None
        S["pool_limit"] = None

        st.markdown(
            "<span class='badge-manual'>⚠️ Mode manuel</span>",
            unsafe_allow_html=True,
        )
        recommended = recommended_matches(
            target,
            MAX_MANUAL_GLOBAL,
        )
        count_key = widget_value(
            "nb_matches", recommended
        )
        count = st.slider(
            "📋 Nombre de rencontres",
            min_value=1,
            max_value=MAX_MANUAL_GLOBAL,
            key=count_key,
        )
        st.caption(
            f"Recommandation indicative : "
            f"{recommended} rencontres."
        )
        S["manual_nb_matches"] = count

        if st.button("➡️ Saisir les matchs"):
            reset_results()
            S["analysis_id"] = str(uuid.uuid4())
            S["matches_pool"] = []
            S["relevant_matches"] = []
            S["manual_draft"] = []
            S["data_info_text"] = ""
            S["data_cotes_text"] = ""
            S["data_extra_text"] = ""
            for key in list(S.keys()):
                if key.startswith((
                    "widget_home_",
                    "widget_away_",
                    "widget_match_sport_",
                )):
                    del S[key]
            for name in (
                "info_text",
                "cotes_text",
                "extra_text",
            ):
                S.pop(f"widget_{name}", None)

            S["step"] = "manual_entry"
            record_progress()
            st.rerun()


# ============================================================
# ÉTAPE 2 — SAISIE MANUELLE
# ============================================================

elif S["step"] == "manual_entry":
    st.markdown("## ✍️ Saisie des matchs")

    if st.button("⬅️ Retour au paramétrage"):
        S["step"] = "setup"
        st.rerun()

    draft = S.get("manual_draft") or []
    matches = []

    for index in range(S["manual_nb_matches"]):
        mid = f"M{index + 1}"
        previous = (
            draft[index]
            if index < len(draft)
            else {}
        )
        st.markdown(f"### {mid}")

        if S["sport"] == "Tout":
            options = SPORT_LIST[:-1]
            initial = previous.get(
                "sport", "Football"
            )
            if initial not in options:
                initial = "Football"
            key = widget_value(
                f"match_sport_{index}", initial
            )
            match_sport = st.selectbox(
                f"Sport de {mid}",
                options,
                key=key,
            )
        else:
            match_sport = (
                "Basketball (Euroligue)"
                if S["sport"] == "Basketball"
                else S["sport"]
            )

        labels = (
            ("Joueur(se) A", "Joueur(se) B")
            if match_sport == "Tennis"
            else ("Équipe A", "Équipe B")
        )
        left, right = st.columns(2)

        with left:
            key = widget_value(
                f"home_{index}",
                previous.get("home", ""),
            )
            home = st.text_input(
                f"{labels[0]} — {mid}",
                key=key,
            )
        with right:
            key = widget_value(
                f"away_{index}",
                previous.get("away", ""),
            )
            away = st.text_input(
                f"{labels[1]} — {mid}",
                key=key,
            )

        matches.append({
            "mid": mid,
            "home": home.strip(),
            "away": away.strip(),
            "sport": match_sport,
            "competition": S["competition"],
            "date": "",
            "status": "en_attente",
        })

    S["manual_draft"] = matches
    complete = all(
        match["home"] and match["away"]
        for match in matches
    )
    if not complete:
        st.warning(
            "Renseignez les deux participants de chaque match."
        )

    if st.button(
        "➡️ Informations complémentaires",
        disabled=not complete,
    ):
        S["matches_pool"] = copy.deepcopy(matches)
        S["relevant_matches"] = copy.deepcopy(matches)
        S["has_match_data"] = True
        S["step"] = "info_request"
        record_progress()
        st.rerun()

    record_progress()


# ============================================================
# ÉTAPE 3 — INFORMATIONS
# ============================================================

elif S["step"] == "info_request":
    st.markdown(
        "## 🔍 Informations complémentaires"
    )
    st.write(
        "Collez ici le rapport préparé pour chaque rencontre. "
        "Utilisez une section commençant par M1:, M2:, etc. "
        "Les informations ne sont pas vérifiées automatiquement."
    )

    if st.button("⬅️ Retour au paramétrage"):
        S["step"] = "setup"
        st.rerun()

    matches = S["relevant_matches"]
    if not matches:
        st.error(
            "Aucun match à analyser. Relancez le paramétrage."
        )
        st.stop()

    for match in matches:
        st.write(
            f"{match['mid']} — {match['home']} contre "
            f"{match['away']} | "
            f"{match.get('competition', '')} | "
            f"{match.get('date', '')}"
        )

    if not S["data_info_text"]:
        S["data_info_text"] = (
            chr(10) * 2
        ).join(
            f"{match['mid']}: "
            for match in matches
        )

    key = widget_value("infotext", S["data_info_text"])

    st.text_area(
        "Une section par match, commençant par M1, M2, etc.",
        height=320,
        key=key,
        on_change=capture_text,
        args=("data_info_text", key),
    )

    S["data_info_text"] = S[key]

    missing = missing_information(
        S["data_info_text"], matches
    )
    if missing:
        st.warning(
            "Informations manquantes ou vides : "
            + ", ".join(missing)
        )

    if st.button(
        "➡️ Générer les options",
        disabled=bool(missing),
    ):
        S["step"] = "options_ready"
        record_progress()
        st.rerun()

    record_progress()
    
# ============================================================
# ÉTAPE 4 — OPTIONS ET COTES
# ============================================================

CONFIDENCE_BADGES = {
    "Excellent": "🟢 Excellent",
    "Bon": "🟡 Bon",
    "Moyen": "🟠 Moyen",
    "Faible": "🔴 Faible",
}


def confidence_badge(option):
    level = option.get("niveau_confiance", "Faible")
    return CONFIDENCE_BADGES.get(level, CONFIDENCE_BADGES["Faible"])


def render_option_card(letter, option):
    if not isinstance(option, dict):
        return

    with st.container(border=True):
        st.markdown(
            f"**{option_label(letter)}** — "
            f"{esc(option_display(option))}"
        )
        st.markdown(confidence_badge(option))

        if option.get("ligne"):
            st.caption(f"Ligne : {esc(option['ligne'])}")

        if option.get("justification"):
            st.caption(
                esc(
                    clean_justification_text(
                        option["justification"]
                    )
                )
            )


def render_extra_card(extra):
    if not isinstance(extra, dict):
        return

    with st.container(border=True):
        st.markdown(
            f"**EXTRA** — {esc(extra.get('description', ''))}"
        )
        st.markdown(confidence_badge(extra))

        if extra.get("cote"):
            st.caption(f"Cote : {extra['cote']}")

        if extra.get("justification"):
            st.caption(
                esc(
                    clean_justification_text(
                        extra["justification"]
                    )
                )
            )


def run_options_analysis():
    matches = S.get("relevant_matches", [])

    if not matches:
        S["options_error"] = (
            "Aucun match sélectionné pour l’analyse."
        )
        return

    info_by_mid = extract_info_by_mid(
        S.get("data_info_text", "")
    )

    options_results = {}
    extra_results = {}
    errors = []

    progress = st.progress(0, text="Analyse des matchs…")

    for index, match in enumerate(matches):
        mid = match.get("mid")
        info_text = info_by_mid.get(mid, "")

        try:
            options_results[mid] = generate_match_options(
                match,
                S.get("data_sport", "Football"),
                info_text,
            )
        except Exception as error:
            options_results[mid] = {
                "synthese": "",
                "_error": str(error)[:250],
                "_diagnostics": [],
            }
            errors.append(f"{mid} : {str(error)[:200]}")

        progress.progress(
            (index + 1) / len(matches),
            text=f"Analyse de {mid}…",
        )

    progress.progress(1.0, text="Analyse terminée.")

    S["options_results"] = options_results
    S["extra_results"] = extra_results
    S["options_error"] = " | ".join(errors)
    S["stage"] = "options_ready"


def run_extra_analysis():
    matches = S.get("relevant_matches", [])
    info_by_mid = extract_info_by_mid(
        S.get("data_info_text", "")
    )

    extra_inputs, extra_errors = parse_extra_input(
        S.get("data_extra_text", "")
    )

    extra_results = {}

    for match in matches:
        mid = match.get("mid")
        extras = extra_inputs.get(mid, [])

        if not extras:
            continue

        evaluated = []

        for extra in extras:
            result = evaluate_extra_market(
                match,
                extra.get("description", ""),
                info_by_mid.get(mid, ""),
            )
            result["description"] = extra.get("description", "")
            result["cote"] = extra.get("cote")
            evaluated.append(result)

        extra_results[mid] = evaluated

    S["extra_results"] = extra_results
    S["extra_error"] = " | ".join(extra_errors)


def render_options_step():
    st.subheader("Étape 4 — Options et cotes")

    matches = S.get("relevant_matches", [])

    if not matches:
        st.warning("Aucun match sélectionné.")
        return

    st.info(
        "L’application propose plusieurs scénarios par match. "
        "Tu peux ensuite saisir les cotes et ajouter des marchés EXTRA."
    )

    if S.get("options_error"):
        st.error(S["options_error"])

    if S.get("extra_error"):
        st.error(S["extra_error"])

    if st.button(
        "Lancer l’analyse des options",
        type="primary",
        use_container_width=True,
    ):
        with st.spinner("Analyse des scénarios…"):
            run_options_analysis()

        if S.get("data_extra_text", "").strip():
            with st.spinner("Analyse des marchés EXTRA…"):
                run_extra_analysis()

        st.rerun()

    options_results = S.get("options_results", {})
    extra_results = S.get("extra_results", {})

    if not options_results:
        return

    st.divider()
    st.markdown("### Scénarios proposés")

    for match in matches:
        mid = match.get("mid")
        result = options_results.get(mid, {})

        with st.container(border=True):
            teams = " vs ".join(
                [
                    match.get("home", ""),
                    match.get("away", ""),
                ]
            )

            st.markdown(
                f"### {mid} — {esc(teams)}"
            )

            if result.get("_error"):
                st.error(result["_error"])
                continue

            if result.get("synthese"):
                st.write(
                    esc(
                        clean_justification_text(
                            result["synthese"]
                        )
                    )
                )

            for letter in ("A", "B", "C", "D", "E"):
                option = result.get(letter)

                if isinstance(option, dict):
                    render_option_card(letter, option)

            for extra in extra_results.get(mid, []):
                render_extra_card(extra)

    st.divider()
    st.markdown("### Saisie des cotes")

    st.caption(
        "Format attendu :

"
        "M1: A=1.56

"
        "M2: A=1.85 B=1.70"
    )

    cotes_key = widget_value(
        "cotes",
        S.get("data_cotes_text", ""),
    )

    cotes_text = st.text_area(
        "Cotes des options",
        value=S.get("data_cotes_text", ""),
        height=180,
        key=cotes_key,
        on_change=capture_text,
        args=("data_cotes_text", cotes_key),
    )

    parsed_cotes, cotes_errors = parse_cotes_input(cotes_text)
    S["data_cotes_parsed"] = parsed_cotes

    if cotes_errors:
        st.error("
".join(cotes_errors))

    st.markdown("### Marchés EXTRA")

    st.caption(
        "Format attendu :

"
        "M1_EXTRA: Total_Under_225.5=1.66

"
        "M2_EXTRA: Joueur_Over_25_points=1.80"
    )

    extra_key = widget_value(
        "extra",
        S.get("data_extra_text", ""),
    )

    extra_text = st.text_area(
        "Marchés EXTRA",
        value=S.get("data_extra_text", ""),
        height=140,
        key=extra_key,
        on_change=capture_text,
        args=("data_extra_text", extra_key),
    )

    if extra_text.strip() and extra_text != S.get(
        "data_extra_text",
        "",
    ):
        if st.button(
            "Analyser les marchés EXTRA",
            use_container_width=True,
        ):
            with st.spinner("Analyse des marchés EXTRA…"):
                run_extra_analysis()

            st.rerun()

    if st.button(
        "Passer à l’étape finale",
        type="primary",
        use_container_width=True,
    ):
        S["stage"] = "final"
        st.rerun()
    

# ============================================================
# ÉTAPE 5 — RÉSULTAT FINAL
# ============================================================

def render_final_selection(selection, match):
    option = selection.get("option", {})
    letter = selection.get("lettre", "")
    cote = selection.get("cote")

    with st.container(border=True):
        teams = " vs ".join(
            [
                match.get("home", ""),
                match.get("away", ""),
            ]
        )

        st.markdown(
            f"**{esc(teams)}** — {esc(mid_label(selection.get('mid')))}"
        )

        if letter == "EXTRA":
            st.markdown(
                f"**Marché EXTRA :** "
                f"{esc(option.get('marche', ''))}"
            )
        else:
            st.markdown(
                f"**{option_label(letter)} :** "
                f"{esc(option_display(option))}"
            )

        st.markdown(confidence_badge(option))

        if valid_odds(cote):
            st.markdown(f"**Cote : {cote}**")

        justification = selection.get("justification") or option.get(
            "justification",
            "",
        )

        if justification:
            st.caption(
                esc(
                    clean_justification_text(justification)
                )
            )


def mid_label(mid):
    for match in S.get("relevant_matches", []):
        if match.get("mid") == mid:
            return (
                str(match.get("home", ""))
                + " vs "
                + str(match.get("away", ""))
            )

    return mid


def run_final_analysis():
    matches = S.get("relevant_matches", [])

    if not matches:
        S["final_error"] = (
            "Aucun match sélectionné pour l’analyse finale."
        )
        return

    cote_cible = S.get("cote_cible", 2.0)
    tolerance = S.get("tolerance", DEFAULT_TOLERANCE)

    selections, synthesis, errors = build_final_coupon(
        matches,
        S.get("options_results", {}),
        S.get("extra_results", {}),
        cote_cible,
        tolerance,
    )

    S["final_result"] = selections
    S["final_justification"] = synthesis
    S["final_error"] = " | ".join(errors)


def render_final_step():
    st.subheader("Étape 5 — Résultat final")

    cote_cible = S.get("cote_cible", 2.0)
    tolerance = S.get("tolerance", DEFAULT_TOLERANCE)

    st.info(
        f"Cote cible : **{cote_cible}** — "
        f"Marge acceptée : **± {tolerance}**"
    )

    if S.get("final_error"):
        st.error(S["final_error"])

    if st.button(
        "Générer le coupon final",
        type="primary",
        use_container_width=True,
    ):
        with st.spinner("Construction du coupon final…"):
            run_final_analysis()

        st.rerun()

    selections = S.get("final_result")

    if not selections:
        st.warning(
            "Aucun coupon final n’a encore été généré."
        )
        return

    total = compute_final_odds(selections)
    min_odds = min_odds_for(len(selections))
    target_min = round(float(cote_cible) - float(tolerance), 2)
    target_max = round(float(cote_cible) + float(tolerance), 2)

    st.markdown("### Coupon proposé")

    if S.get("final_justification"):
        st.write(
            esc(
                clean_justification_text(
                    S["final_justification"]
                )
            )
        )

    matches_by_mid = {
        match.get("mid"): match
        for match in S.get("relevant_matches", [])
    }

    for selection in selections:
        match = matches_by_mid.get(
            selection.get("mid"),
            {},
        )
        render_final_selection(selection, match)

    st.divider()

    col1, col2, col3 = st.columns(3)

    with col1:
        st.metric("Nombre de sélections", len(selections))

    with col2:
        st.metric("Cote totale", total)

    with col3:
        st.metric("Cote minimale", min_odds)

    if total < target_min or total > target_max:
        st.warning(
            f"La cote totale est hors de la plage demandée : "
            f"{target_min} à {target_max}."
        )
    else:
        st.success(
            "Le coupon respecte la cote cible et les règles de cotes."
        )

    st.divider()

    if st.button(
        "Recalculer le coupon",
        use_container_width=True,
    ):
        with st.spinner("Nouvelle recherche de coupon…"):
            run_final_analysis()

        st.rerun()

    if st.button(
        "Retour à l’étape 4",
        use_container_width=True,
    ):
        S["stage"] = "options_ready"
        st.rerun()
