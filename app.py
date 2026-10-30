import streamlit as st

# --- Pont de compatibilité pour streamlit-cookies-manager ---
if not hasattr(st, "cache"):
    def _legacy_cache_compat(func=None, **kwargs):
        for bad_kwarg in ("allow_output_mutation", "hash_funcs", "suppress_st_warning"):
            kwargs.pop(bad_kwarg, None)
        if func is not None:
            return st.cache_resource(func)
        return st.cache_resource(**kwargs)
    st.cache = _legacy_cache_compat

from streamlit_cookies_manager import EncryptedCookieManager
import hashlib
import secrets as secrets_lib
import json
import math
import re
import itertools
import requests
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai

# ============================================================
# CONFIGURATION GENERALE / CSS
# ============================================================
st.set_page_config(page_title="Œil de l'Aigle", page_icon="🦅", layout="centered")

st.markdown("""
<style>
html, body, [class*="css"] { font-size: 16px !important; }
p, li, span, div { line-height: 1.7 !important; }
</style>
""", unsafe_allow_html=True)

def display_aerated(text: str):
    """Affiche un texte en paragraphes aérés, sans markdown de titre ni bloc code."""
    if not text:
        return
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    for p in paragraphs:
        st.write(p)
        st.write("")

def clean_justification_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\{.*?\}", "", text, flags=re.DOTALL)
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"[{}\[\]]", "", text)
    return text.strip()

# ============================================================
# GOOGLE SHEETS - CLIENT & ACCES ONGLETS
# ============================================================
USERS_SHEET_NAME = "Users"
HISTORY_SHEET_NAME = "History"
PASSWORD_SALT = "oeildelaigle_salt_2024"
MAX_HISTORY_PER_USER = 50

@st.cache_resource
def get_gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=scopes
    )
    return gspread.authorize(creds)

def get_sheet(sheet_name: str):
    client = get_gspread_client()
    spreadsheet = client.open_by_url(st.secrets["GOOGLE_SHEET_URL"])
    return spreadsheet.worksheet(sheet_name)

# ============================================================
# AUTHENTIFICATION - FONCTIONS UTILISATEURS
# ============================================================
def hash_password(password: str) -> str:
    return hashlib.sha256((password + PASSWORD_SALT).encode("utf-8")).hexdigest()

def find_user_row(email: str):
    sheet = get_sheet(USERS_SHEET_NAME)
    values = sheet.get_all_values()
    for idx, row in enumerate(values, start=1):
        if idx == 1:
            continue
        if row and row[0].strip().lower() == email.strip().lower():
            return idx, row
    return None, None

def create_user(email: str, password: str):
    existing_idx, _ = find_user_row(email)
    if existing_idx:
        return False, "Un compte existe déjà avec cet email."
    sheet = get_sheet(USERS_SHEET_NAME)
    sheet.append_row([
        email.strip(),
        hash_password(password),
        datetime.now().isoformat(),
        "",
        ""
    ])
    return True, "Compte créé avec succès."

def verify_login(email: str, password: str):
    idx, row = find_user_row(email)
    if not idx:
        return False
    stored_hash = row[1] if len(row) > 1 else ""
    return stored_hash == hash_password(password)

def set_session_token(email: str) -> str:
    idx, row = find_user_row(email)
    if not idx:
        return ""
    token = secrets_lib.token_hex(32)
    sheet = get_sheet(USERS_SHEET_NAME)
    sheet.update_cell(idx, 5, token)
    return token

def check_session_token(token: str):
    if not token:
        return None
    sheet = get_sheet(USERS_SHEET_NAME)
    values = sheet.get_all_values()
    for idx, row in enumerate(values, start=1):
        if idx == 1:
            continue
        if len(row) > 4 and row[4].strip() == token.strip():
            return row[0]
    return None

def clear_session_token(email: str):
    idx, row = find_user_row(email)
    if not idx:
        return
    sheet = get_sheet(USERS_SHEET_NAME)
    sheet.update_cell(idx, 5, "")

# ============================================================
# HISTORIQUE : FONCTIONS TECHNIQUES
# ============================================================
def classify_period(dt: datetime) -> str:
    now = datetime.now()
    today = now.date()
    d = dt.date()
    if d == today:
        return "Aujourd'hui"
    if d == today - timedelta(days=1):
        return "Hier"
    if d >= today - timedelta(days=7):
        return "Cette semaine"
    if d.year == today.year and d.month == today.month:
        return "Ce mois-ci"
    return "Plus ancien"

def save_history_entry(user_email: str, stage: str, sport: str, cote_cible, session_data: dict):
    """Ajoute une entrée d'historique pour l'utilisateur. N'interrompt jamais le flux principal
    de l'app en cas d'erreur (ex: Google Sheets temporairement indisponible)."""
    try:
        sheet = get_sheet(HISTORY_SHEET_NAME)
        safe_data = {}
        for k, v in (session_data or {}).items():
            if isinstance(v, (str, int, float, bool, list, dict)) or v is None:
                safe_data[k] = v
        row = [
            user_email.strip(),
            datetime.now().isoformat(),
            stage or "",
            sport or "",
            str(cote_cible) if cote_cible is not None else "",
            json.dumps(safe_data, ensure_ascii=False)
        ]
        sheet.append_row(row)
        _prune_old_history(user_email)
    except Exception:
        pass

def _prune_old_history(user_email: str):
    sheet = get_sheet(HISTORY_SHEET_NAME)
    values = sheet.get_all_values()
    user_rows = []
    for idx, row in enumerate(values, start=1):
        if idx == 1:
            continue
        if row and row[0].strip().lower() == user_email.strip().lower():
            timestamp = row[1] if len(row) > 1 else ""
            user_rows.append((idx, timestamp))
    if len(user_rows) <= MAX_HISTORY_PER_USER:
        return
    user_rows.sort(key=lambda x: x[1])
    nb_to_delete = len(user_rows) - MAX_HISTORY_PER_USER
    rows_to_delete = [r[0] for r in user_rows[:nb_to_delete]]
    delete_history_rows(rows_to_delete)

def get_user_history(user_email: str):
    sheet = get_sheet(HISTORY_SHEET_NAME)
    values = sheet.get_all_values()
    entries = []
    for idx, row in enumerate(values, start=1):
        if idx == 1:
            continue
        if not row or len(row) < 1:
            continue
        if row[0].strip().lower() != user_email.strip().lower():
            continue
        try:
            ts = datetime.fromisoformat(row[1]) if len(row) > 1 and row[1] else None
        except ValueError:
            ts = None
        try:
            data = json.loads(row[5]) if len(row) > 5 and row[5] else {}
        except (json.JSONDecodeError, IndexError):
            data = {}
        entries.append({
            "row_index": idx,
            "timestamp": ts,
            "stage": row[2] if len(row) > 2 else "",
            "sport": row[3] if len(row) > 3 else "",
            "cote_cible": row[4] if len(row) > 4 else "",
            "session_data": data
        })
    entries.sort(key=lambda e: e["timestamp"] or datetime.min, reverse=True)
    return entries

def delete_history_rows(row_indices: list):
    if not row_indices:
        return
    sheet = get_sheet(HISTORY_SHEET_NAME)
    for idx in sorted(set(row_indices), reverse=True):
        try:
            sheet.delete_rows(idx)
        except Exception:
            pass

def delete_all_history(user_email: str):
    entries = get_user_history(user_email)
    row_indices = [e["row_index"] for e in entries]
    delete_history_rows(row_indices)

# ============================================================
# CONSTANTES DE CALIBRATION (Phase 1, validées)
# ============================================================
MAX_INFO_MATCHES = 5
MAX_POOL_SIMPLE_COMBO = 10
MAX_POOL_MULTI_AUTO = 15
MAX_MANUAL_GLOBAL = 20
DEFAULT_CONFIDENCE_THRESHOLD = 0.70
HIGH_ODDS_THRESHOLD = 15.0
HIGH_ODDS_CONFIDENCE = 0.60

def estimated_selections(cote_cible):
    return max(2, round(math.log(cote_cible) / math.log(1.7)))

def validity_rate(cote_cible):
    return HIGH_ODDS_CONFIDENCE if cote_cible >= HIGH_ODDS_THRESHOLD else DEFAULT_CONFIDENCE_THRESHOLD

def recommended_manual_matches(cote_cible):
    sel = estimated_selections(cote_cible)
    rate = validity_rate(cote_cible)
    rec = math.ceil(sel / rate)
    return min(MAX_MANUAL_GLOBAL, max(sel, rec))

def determine_mode_params(cote_cible):
    if cote_cible <= 3.00:
        mode, coupon_max_size, jours, limit = "simple_double", 2, 2, MAX_POOL_SIMPLE_COMBO
    else:
        estimated = estimated_selections(cote_cible)
        if estimated <= 5:
            mode, coupon_max_size, jours, limit = "combo_info", estimated, 7, MAX_POOL_SIMPLE_COMBO
        else:
            mode, coupon_max_size, jours, limit = "multi", None, 7, MAX_POOL_MULTI_AUTO
    threshold = validity_rate(cote_cible)
    return mode, coupon_max_size, jours, limit, threshold

# ============================================================
# APIS SPORTIVES (football-data.org / balldontlie.io)
# ============================================================
FOOTBALL_COMPETITIONS = {
    "Premier League": "PL", "Ligue 1": "FL1", "Liga": "PD", "Serie A": "SA",
    "Bundesliga": "BL1", "Ligue des Champions": "CL", "Championship": "ELC",
    "Eredivisie": "DED", "Primeira Liga": "PPL", "Brasileirão": "BSA"
}

def fetch_football_matches(competition_code, date_from, date_to):
    headers = {"X-Auth-Token": st.secrets["FOOTBALL_API_KEY"]}
    results = []
    codes = list(FOOTBALL_COMPETITIONS.values()) if competition_code == "TOUT" else [competition_code]
    for code in codes:
        try:
            url = f"https://api.football-data.org/v4/competitions/{code}/matches"
            params = {"dateFrom": date_from, "dateTo": date_to}
            r = requests.get(url, headers=headers, params=params, timeout=10)
            if r.status_code == 200:
                for m in r.json().get("matches", []):
                    results.append({
                        "label": f"{m['homeTeam']['name']} vs {m['awayTeam']['name']} ({m['utcDate'][:10]})",
                        "raw": m
                    })
        except requests.RequestException:
            continue
    return results

def fetch_nba_matches(date_from, date_to):
    headers = {"Authorization": st.secrets["BASKETBALL_API_KEY"]}
    results = []
    try:
        url = "https://api.balldontlie.io/v1/games"
        params = {"start_date": date_from, "end_date": date_to, "per_page": 25}
        r = requests.get(url, headers=headers, params=params, timeout=10)
        if r.status_code == 200:
            for g in r.json().get("data", []):
                home = g.get("home_team", {}).get("full_name", "Équipe A")
                away = g.get("visitor_team", {}).get("full_name", "Équipe B")
                date = g.get("date", "")[:10]
                results.append({"label": f"{home} vs {away} ({date})", "raw": g})
    except requests.RequestException:
        pass
    return results

# ============================================================
# IA GEMINI - CONFIGURATION ET PROMPTS
# ============================================================
genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])
GEMINI_MODEL = "gemini-flash-lite-latest"

NO_MARKDOWN_BASE = (
    "Réponds en texte fluide, en français, sans titres markdown, sans astérisques, "
    "sans listes à puces, sans blocs de code, sans accolades JSON dans le texte narratif. "
)

def call_gemini(prompt: str) -> str:
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        response = model.generate_content(prompt)
        return response.text or ""
    except Exception as e:
        return f"[Erreur IA temporaire: {e}]"

MARKETS_BY_SPORT = {
    "Football": ["1X2", "Double Chance", "BTTS", "Over/Under buts (1.5/2.5/3.5)", "Total individuel équipe", "Handicap asiatique simple"],
    "Basketball": ["Victoire", "Handicap/Spread", "Over/Under points total", "Total individuel équipe"],
    "Tennis": ["Vainqueur du match", "Total jeux Over/Under", "Handicap en jeux"],
    "Hockey sur glace": ["Vainqueur (incl. prolongation)", "Total buts Over/Under", "Puck line (handicap)"],
    "Football américain": ["Vainqueur (Moneyline)", "Spread/Handicap", "Total points Over/Under"],
}

MID_REGEX = re.compile(r"(M\d+)\s*:")

def build_options_prompt(mid, label, sport, info_text, is_manual):
    base_markets = MARKETS_BY_SPORT.get(sport, MARKETS_BY_SPORT["Football"])
    data_note = (
        "Aucune donnée vérifiée via API n'est disponible pour ce match (mode manuel) : "
        "base-toi uniquement sur les informations complémentaires fournies ci-dessous par l'utilisateur, "
        "sans inventer de statistiques précises."
        if is_manual else
        "Ce match provient d'une base de données sportive réelle. Base-toi sur les informations factuelles "
        "disponibles (forme, contexte de compétition) sans inventer de statistiques précises non vérifiables."
    )
    info_block = f"Informations complémentaires fournies par l'utilisateur : {info_text}" if info_text else "Aucune information complémentaire fournie."
    return (
        NO_MARKDOWN_BASE +
        f"Pour le match {label} ({sport}), propose EXACTEMENT 3 options de marché parmi celles-ci : "
        f"{', '.join(base_markets)}. {data_note} {info_block} "
        "Rédige d'abord un court texte explicatif (3-4 phrases) sur l'état de ce match et pourquoi ces 3 options "
        "sont pertinentes. Ensuite, écris exactement la mention ###DATA_BLOCK### suivie d'un JSON valide de la "
        "forme [{\"option\":\"A\",\"marche\":\"...\",\"probabilite\":0.58}, {\"option\":\"B\",...}, {\"option\":\"C\",...}] "
        "avec des probabilités entre 0 et 1 qui reflètent ta confiance réelle, sans inventer de précision excessive."
    )

def parse_options_response(raw_text):
    if "###DATA_BLOCK###" not in raw_text:
        return raw_text.strip(), []
    narrative, data_part = raw_text.split("###DATA_BLOCK###", 1)
    data_part = data_part.strip()
    data_part = re.sub(r"```json|```", "", data_part).strip()
    try:
        options = json.loads(data_part)
    except json.JSONDecodeError:
        options = []
    return narrative.strip(), options

def build_reliability_prompt(matches_labels, sport):
    joined = "; ".join(matches_labels)
    return (
        NO_MARKDOWN_BASE +
        f"Voici une liste de matchs de {sport} : {joined}. "
        "Évalue pour chacun un score de fiabilité de 0 à 100 représentant à quel point on dispose "
        "d'informations suffisamment claires pour analyser ce match sérieusement (forme des équipes, enjeu, "
        "contexte de compétition), sans texte explicatif. "
        "Réponds uniquement avec la mention ###DATA_BLOCK### suivie d'un JSON de la forme "
        "[{\"label\":\"...\",\"score\":75}, ...] dans le même ordre que la liste fournie."
    )

def rank_matches_by_reliability(matches, sport):
    labels = [m["label"] for m in matches]
    raw = call_gemini(build_reliability_prompt(labels, sport))
    _, data = parse_options_response(raw)
    scores = {}
    for d in data:
        scores[d.get("label")] = d.get("score", 50)
    for m in matches:
        m["reliability"] = scores.get(m["label"], 50)
    return sorted(matches, key=lambda m: m["reliability"], reverse=True)

def build_extra_eval_prompt(label, sport, extra_name, info_text):
    info_block = f"Informations complémentaires : {info_text}" if info_text else ""
    return (
        NO_MARKDOWN_BASE +
        f"Pour le match {label} ({sport}), évalue le marché personnalisé suivant proposé par l'utilisateur : "
        f"{extra_name.replace('_', ' ')}. {info_block} "
        "Donne une probabilité réaliste de réalisation de ce scénario, sans texte explicatif. "
        "Réponds uniquement avec ###DATA_BLOCK### suivie d'un JSON de la forme {\"probabilite\":0.45}."
    )

def evaluate_extra_market(label, sport, extra_name, info_text):
    raw = call_gemini(build_extra_eval_prompt(label, sport, extra_name, info_text))
    _, data = parse_options_response(raw)
    if isinstance(data, dict):
        return data.get("probabilite", 0.5)
    return 0.5

def build_justification_prompt(coupon_matches, cote_cible, cote_obtenue):
    details = []
    for m in coupon_matches:
        details.append(f"{m['label']} : option retenue {m['selected_market']} (probabilité estimée {m['selected_probability']:.0%}, cote {m['selected_odds']})")
    details_text = " ; ".join(details)
    return (
        NO_MARKDOWN_BASE +
        f"Voici le coupon final retenu pour une cote cible de {cote_cible} (cote obtenue : {cote_obtenue:.2f}) : {details_text}. "
        "Rédige un texte fluide de 3 à 4 phrases expliquant pourquoi ces options retenues représentent le scénario "
        "le plus probable par rapport aux autres options qui avaient été évaluées pour ces mêmes matchs, "
        "sans accolades, sans JSON, sans liste à puces."
    )

# ============================================================
# HISTORIQUE : INSTANTANE DE LA SESSION EN COURS
# ============================================================
def snapshot_session_data() -> dict:
    """Rassemble toutes les données nécessaires pour reprendre une analyse plus tard."""
    return {
        "analysis_id": st.session_state.get("current_analysis_id"),
        "mode": st.session_state.get("mode"),
        "coupon_max_size": st.session_state.get("coupon_max_size"),
        "jours_fenetre": st.session_state.get("jours_fenetre"),
        "pool_limit": st.session_state.get("pool_limit"),
        "threshold": st.session_state.get("threshold"),
        "competition": st.session_state.get("competition"),
        "cote_cible": st.session_state.get("cote_cible"),
        "nb_tout": st.session_state.get("nb_tout"),
        "is_auto": st.session_state.get("is_auto"),
        "match_pool": st.session_state.get("match_pool"),
        "generated_options": st.session_state.get("generated_options"),
        "data_manual_text": st.session_state.get("data_manual_text"),
        "data_info_text": st.session_state.get("data_info_text"),
        "data_cotes_text": st.session_state.get("data_cotes_text"),
        "cotes_text_saved": st.session_state.get("cotes_text_saved"),
    }

def record_progress(stage: str):
    """Enregistre discrètement la progression actuelle dans l'Historique."""
    user_email = st.session_state.get("user_email")
    if not user_email:
        return
    save_history_entry(
        user_email,
        stage,
        st.session_state.get("sport", ""),
        st.session_state.get("cote_cible"),
        snapshot_session_data()
    )

# ============================================================
# COOKIE MANAGER + PORTAIL DE CONNEXION
# ============================================================
cookies = EncryptedCookieManager(prefix="oeildelaigle_", password=st.secrets["COOKIE_PASSWORD"])
if not cookies.ready():
    st.stop()

if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False
    st.session_state["user_email"] = None

if not st.session_state["logged_in"]:
    token = cookies.get("session_token")
    if token:
        email_from_token = check_session_token(token)
        if email_from_token:
            st.session_state["logged_in"] = True
            st.session_state["user_email"] = email_from_token

if not st.session_state["logged_in"]:
    st.title("🦅 Œil de l'Aigle")
    st.caption("L'algorithme qui analyse les vraies données sportives pour éclairer tes choix de paris.")
    st.subheader("Connexion")

    mode_login = st.radio("", ["Se connecter", "Créer un compte"], horizontal=True, label_visibility="collapsed")

    email_input = st.text_input("Email", placeholder="Ex: jeandupont@gmail.com", key="login_email")
    password_input = st.text_input("Mot de passe", type="password", placeholder="Ex: jeandupont1234", key="login_password")

    fields_filled = bool(email_input.strip()) and bool(password_input.strip())

    if mode_login == "Créer un compte":
        if st.button("Créer mon compte", disabled=not fields_filled):
            ok, msg = create_user(email_input, password_input)
            if ok:
                st.success(msg + " Tu peux maintenant te connecter.")
            else:
                st.error(msg)
        if not fields_filled:
            st.caption("Remplis les deux champs pour activer le bouton.")
    else:
        if st.button("Se connecter", disabled=not fields_filled):
            if verify_login(email_input, password_input):
                token = set_session_token(email_input)
                cookies["session_token"] = token
                cookies.save()
                st.session_state["logged_in"] = True
                st.session_state["user_email"] = email_input.strip()
                st.rerun()
            else:
                st.error("Email ou mot de passe incorrect.")
        if not fields_filled:
            st.caption("Remplis les deux champs pour activer le bouton.")

    st.stop()

# ============================================================
# BARRE LATERALE
# ============================================================
with st.sidebar:
    st.write(f"👤 Connecté : **{st.session_state['user_email']}**")
    if st.button("🚪 Se déconnecter"):
        clear_session_token(st.session_state["user_email"])
        cookies["session_token"] = ""
        cookies.save()
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()

# ============================================================
# INITIALISATION DE L'ETAT DE L'APPLICATION
# ============================================================
if "stage" not in st.session_state:
    st.session_state["stage"] = "setup"

def ensure_default(key, default):
    if key not in st.session_state:
        st.session_state[key] = default

ensure_default("data_sport", "Football")
ensure_default("data_competition", None)
ensure_default("data_cote_cible", 1.50)
ensure_default("data_nb_tout", None)
ensure_default("data_manual_text", "")
ensure_default("data_info_text", "")
ensure_default("data_cotes_text", "")
ensure_default("match_pool", [])
ensure_default("generated_options", {})
ensure_default("current_analysis_id", None)

st.title("🦅 Œil de l'Aigle")

# ============================================================
# ETAPE: SETUP (page d'accueil)
# ============================================================
if st.session_state["stage"] == "setup":
    st.markdown("### Vois le match comme un aigle voit sa proie.")
    st.write(
        "Œil de l'Aigle analyse de vraies données sportives — championnats de football et NBA en direct — "
        "pour identifier les scénarios les plus probables selon la cote que tu vises. Choisis ton sport, "
        "indique ta cote cible, et laisse l'algorithme sélectionner les matchs et les marchés les plus fiables."
    )
    st.write("")

    sport_options = ["Football", "Basketball", "Tennis", "Hockey sur glace", "Football américain", "Tout"]
    sport_index = sport_options.index(st.session_state["data_sport"]) if st.session_state["data_sport"] in sport_options else 0
    sport = st.selectbox("Sport", sport_options, index=sport_index, key="setup_sport_widget")

    sport_changed = sport != st.session_state["data_sport"]
    st.session_state["data_sport"] = sport

    has_match_data = (
        bool(st.session_state.get("data_manual_text", "").strip())
        or bool(st.session_state.get("match_pool"))
        or bool(st.session_state.get("data_info_text", "").strip())
        or bool(st.session_state.get("data_cotes_text", "").strip())
    )

    if sport_changed and has_match_data:
        for widget_key in ("setup_competition_widget", "manual_text_widget", "info_text_widget", "cotes_text_widget"):
            st.session_state.pop(widget_key, None)
        st.session_state["data_competition"] = None
        st.session_state["data_manual_text"] = ""
        st.session_state["data_info_text"] = ""
        st.session_state["data_cotes_text"] = ""
        st.session_state["match_pool"] = []
        st.session_state["generated_options"] = {}
        st.info("Le sport a changé : les matchs précédemment saisis ont été réinitialisés.")
    elif sport_changed and not has_match_data:
        st.session_state.pop("setup_competition_widget", None)
        st.session_state["data_competition"] = None

    competition = None
    if sport == "Football":
        comp_options = list(FOOTBALL_COMPETITIONS.keys()) + ["Tout"]
        comp_index = comp_options.index(st.session_state["data_competition"]) if st.session_state["data_competition"] in comp_options else 0
        competition = st.selectbox("Compétition", comp_options, index=comp_index, key="setup_competition_widget")
        st.session_state["data_competition"] = competition
    elif sport == "Basketball":
        comp_options = ["NBA (auto)", "Euroligue (manuel)"]
        comp_index = comp_options.index(st.session_state["data_competition"]) if st.session_state["data_competition"] in comp_options else 0
        competition = st.selectbox("Compétition", comp_options, index=comp_index, key="setup_competition_widget")
        st.session_state["data_competition"] = competition

    cote_cible = st.number_input(
        "Cote cible", min_value=1.01, step=0.01,
        value=float(st.session_state["data_cote_cible"]),
        key="setup_cote_widget"
    )
    st.session_state["data_cote_cible"] = cote_cible

    is_auto = (sport == "Football" and competition != "Tout") or (sport == "Basketball" and competition == "NBA (auto)")
    is_manual_needed = not is_auto

    if is_auto:
        st.success("✅ Données vérifiées — matchs sélectionnés automatiquement depuis une base de données réelle.")
    else:
        st.warning("⚠️ Mode manuel — tu devras saisir toi-même les matchs à analyser.")

    nb_tout = None
    if is_manual_needed and cote_cible:
        reco = recommended_manual_matches(cote_cible)
        default_nb = st.session_state["data_nb_tout"] if st.session_state["data_nb_tout"] else reco
        default_nb = min(max(int(default_nb), 1), MAX_MANUAL_GLOBAL)
        st.caption(f"Nombre de matchs recommandé pour cette cote : {reco} (plafond {MAX_MANUAL_GLOBAL})")
        nb_tout = st.slider("Nombre de matchs à inclure", min_value=1, max_value=MAX_MANUAL_GLOBAL, value=default_nb, key="setup_nb_widget")
        st.session_state["data_nb_tout"] = nb_tout

        if st.session_state.get("data_manual_text", "").strip():
            with st.expander("📝 Matchs déjà saisis précédemment (tu pourras les compléter à l'étape suivante)"):
                st.text(st.session_state["data_manual_text"])

    if st.button("🚀 Lancer l'analyse", disabled=not cote_cible):
        mode, coupon_max_size, jours, limit, threshold = determine_mode_params(cote_cible)
        st.session_state["mode"] = mode
        st.session_state["coupon_max_size"] = coupon_max_size if coupon_max_size else nb_tout
        st.session_state["jours_fenetre"] = jours
        st.session_state["pool_limit"] = limit
        st.session_state["threshold"] = threshold
        st.session_state["sport"] = sport
        st.session_state["competition"] = competition
        st.session_state["cote_cible"] = cote_cible
        st.session_state["nb_tout"] = nb_tout
        st.session_state["is_auto"] = is_auto
        st.session_state["current_analysis_id"] = secrets_lib.token_hex(8)

        if is_auto:
            date_from = datetime.now().strftime("%Y-%m-%d")
            date_to = (datetime.now() + timedelta(days=jours)).strftime("%Y-%m-%d")
            if sport == "Football":
                code = FOOTBALL_COMPETITIONS[competition]
                fetched = fetch_football_matches(code, date_from, date_to)
            else:
                fetched = fetch_nba_matches(date_from, date_to)
            fetched = fetched[:limit]
            pool = []
            for i, m in enumerate(fetched, start=1):
                pool.append({"mid": f"M{i}", "label": m["label"], "info": "", "kept": True})

            if mode in ("simple_double", "combo_info") and len(pool) > MAX_INFO_MATCHES:
                ranked = rank_matches_by_reliability(pool, sport)
                for i, m in enumerate(ranked):
                    m["kept"] = i < MAX_INFO_MATCHES
                if mode == "simple_double":
                    pool = [m for m in ranked if m["kept"]]
                else:
                    pool = ranked

            st.session_state["match_pool"] = pool
            st.session_state["stage"] = "info_request" if mode != "multi" else "options_ready"
        else:
            st.session_state["stage"] = "manual_entry"

        record_progress(st.session_state["stage"])
        st.rerun()

# ============================================================
# ETAPE: MANUAL_ENTRY (saisie manuelle des matchs - mode manuel uniquement)
# ============================================================
elif st.session_state["stage"] == "manual_entry":
    st.subheader("Saisie manuelle des matchs")
    st.warning("⚠️ Mode manuel — vérifie toi-même les informations des matchs saisis, aucune donnée API n'est utilisée.")

    if st.button("⬅️ Retour à l'accueil"):
        st.session_state["stage"] = "setup"
        st.rerun()

    nb = st.session_state.get("nb_tout") or st.session_state.get("data_nb_tout") or 5
    sport_label = st.session_state.get("sport", "")
    st.caption(
        f"Saisis jusqu'à {nb} matchs, un par ligne, au format : M1: Équipe A vs Équipe B - Date"
        + (" - Sport" if sport_label == "Tout" else "")
    )
    if st.session_state.get("data_manual_text", "").strip():
        st.caption("Tes matchs précédemment saisis ont été conservés ci-dessous. Modifie ou complète la liste si besoin.")

    manual_text = st.text_area(
        "Liste des matchs",
        value=st.session_state.get("data_manual_text", ""),
        height=220,
        key="manual_text_widget"
    )
    st.session_state["data_manual_text"] = manual_text

    if st.button("Valider les matchs", disabled=not manual_text.strip()):
        mids_found = MID_REGEX.findall(manual_text)
        lines = manual_text.split("\n")
        pool = []
        for mid in mids_found:
            for line in lines:
                if line.strip().startswith(mid + ":"):
                    pool.append({"mid": mid, "label": line.strip()[len(mid) + 1:].strip(), "info": "", "kept": True})
                    break
        st.session_state["match_pool"] = pool
        st.session_state["stage"] = "info_request"
        record_progress("info_request")
        st.rerun()

# ============================================================
# ETAPE: INFO_REQUEST (demande d'infos complémentaires - une seule vague)
# ============================================================
elif st.session_state["stage"] == "info_request":
    st.subheader("Informations complémentaires (facultatif mais recommandé)")

    if st.button("⬅️ Retour à l'accueil"):
        st.session_state["stage"] = "setup"
        st.rerun()

    pool = st.session_state.get("match_pool", [])
    active_matches = [m for m in pool if m.get("kept", True)]

    if not active_matches:
        st.warning("Aucun match actif trouvé. Reviens à l'accueil pour recommencer.")
    else:
        st.write(
            "Tu peux enrichir l'analyse en collant ici des informations trouvées ailleurs (blessures, forme du "
            "moment, compositions probables, météo, etc.) via le site de ton choix (ex: Score24, Flashscore). "
            "Cette étape n'aura lieu qu'une seule fois."
        )
        for m in active_matches:
            st.caption(f"{m['mid']} — {m['label']}")

        info_text = st.text_area(
            "Colle tes informations complémentaires au format : M1: texte libre / M2: texte libre",
            value=st.session_state.get("data_info_text", ""),
            height=180,
            key="info_text_widget"
        )
        st.session_state["data_info_text"] = info_text

        if st.button("Continuer vers les options de marché ➡️"):
            info_map = {}
            for mid in [m["mid"] for m in active_matches]:
                match_found = re.search(rf"{mid}\s*:(.*?)(?=M\d+\s*:|$)", info_text or "", flags=re.DOTALL)
                info_map[mid] = match_found.group(1).strip() if match_found else ""
            for m in pool:
                if m["mid"] in info_map:
                    m["info"] = info_map[m["mid"]]
            st.session_state["match_pool"] = pool
            st.session_state["stage"] = "options_ready"
            record_progress("options_ready")
            st.rerun()

# ============================================================
# ETAPE: OPTIONS_READY (3 options IA par match + saisie des cotes réelles)
# ============================================================
elif st.session_state["stage"] == "options_ready":
    st.subheader("Options de marché et saisie des cotes")

    sport = st.session_state.get("sport", "Football")
    is_auto = st.session_state.get("is_auto", False)
    pool = st.session_state.get("match_pool", [])

    if is_auto:
        st.success("✅ Données vérifiées")
    else:
        st.warning("⚠️ Mode manuel")

    with st.spinner("Génération des options de marché par l'IA (peut prendre quelques minutes)..."):
        for m in pool:
            if m["mid"] not in st.session_state["generated_options"]:
                match_sport = sport if sport != "Tout" else "Football"
                raw = call_gemini(build_options_prompt(m["mid"], m["label"], match_sport, m.get("info", ""), not is_auto))
                narrative, options = parse_options_response(raw)
                st.session_state["generated_options"][m["mid"]] = {
                    "narrative": narrative, "options": options, "label": m["label"], "sport": match_sport
                }

    for m in pool:
        data = st.session_state["generated_options"].get(m["mid"], {})
        st.markdown(f"**{m['mid']} — {data.get('label', m['label'])}**")
        display_aerated(data.get("narrative", ""))
        for opt in data.get("options", []):
            st.write(f"Option {opt.get('option', '?')} : {opt.get('marche', '?')} (probabilité estimée {opt.get('probabilite', 0):.0%})")
        st.write("---")

    st.info("Indique les cotes réelles de ton bookmaker pour chaque match, au format : M1: A=1.85 B=1.70 C=1.90")
    st.caption("Marchés personnalisés optionnels : EXTRA: Nom_du_marche=cote,Autre_marche=cote (sans espace ni virgule dans les noms)")

    cotes_text = st.text_area(
        "Cotes",
        value=st.session_state.get("data_cotes_text", ""),
        height=200,
        key="cotes_text_widget"
    )
    st.session_state["data_cotes_text"] = cotes_text

    required_mids = set(m["mid"] for m in pool)
    provided_mids = set(MID_REGEX.findall(cotes_text))
    missing = required_mids - provided_mids

    if missing:
        st.warning(f"Il manque les cotes pour : {', '.join(sorted(missing))}")

    if st.button("✅ Analyser les cotes et finaliser", disabled=bool(missing) or not cotes_text.strip()):
        st.session_state["cotes_text_saved"] = cotes_text
        st.session_state["stage"] = "final"
        record_progress("final")
        st.rerun()

# ============================================================
# ETAPE: FINAL (sélection combinatoire + justification + récapitulatif)
# ============================================================
elif st.session_state["stage"] == "final":
    st.subheader("Coupon final")

    pool = st.session_state.get("match_pool", [])
    generated = st.session_state.get("generated_options", {})
    cotes_text = st.session_state.get("cotes_text_saved", "")
    threshold = st.session_state.get("threshold", 0.70)
    coupon_max_size = st.session_state.get("coupon_max_size") or 5
    cote_cible = st.session_state.get("cote_cible", 1.5)
    sport = st.session_state.get("sport", "Football")

    match_odds_blocks = re.split(r"(M\d+)\s*:", cotes_text)
    odds_map = {}
    for i in range(1, len(match_odds_blocks), 2):
        mid = match_odds_blocks[i]
        block = match_odds_blocks[i + 1] if i + 1 < len(match_odds_blocks) else ""
        odds_map[mid] = block.strip()

    results = []
    for m in pool:
        mid = m["mid"]
        data = generated.get(mid, {})
        options = data.get("options", [])
        block = odds_map.get(mid, "")

        if not block:
            results.append({"mid": mid, "label": m["label"], "status": "Aucune cote fournie"})
            continue

        candidates = []
        for letter_match in re.finditer(r"([A-Z])\s*=\s*([\d.]+)", block):
            letter, odds_val = letter_match.group(1), float(letter_match.group(2))
            opt = next((o for o in options if o.get("option") == letter), None)
            if opt:
                candidates.append({"market": opt.get("marche", letter), "probability": opt.get("probabilite", 0), "odds": odds_val})

        extra_match = re.search(r"EXTRA\s*:\s*(.+)", block)
        if extra_match:
            extras = extra_match.group(1).split(",")
            for extra in extras:
                if "=" in extra:
                    name, val = extra.split("=", 1)
                    name = name.strip()
                    try:
                        odds_val = float(val.strip())
                    except ValueError:
                        continue
                    prob = evaluate_extra_market(m["label"], data.get("sport", sport), name, m.get("info", ""))
                    candidates.append({"market": name.replace("_", " "), "probability": prob, "odds": odds_val})

        if not candidates:
            results.append({"mid": mid, "label": m["label"], "status": "Aucune option exploitable"})
            continue

        best = max(candidates, key=lambda c: c["probability"])
        if best["probability"] >= threshold:
            results.append({
                "mid": mid, "label": m["label"], "status": "Retenu (en attente)",
                "selected_market": best["market"], "selected_probability": best["probability"],
                "selected_odds": best["odds"]
            })
        else:
            results.append({
                "mid": mid, "label": m["label"], "status": "Écarté",
                "selected_market": best["market"], "selected_probability": best["probability"],
                "selected_odds": best["odds"]
            })

    validated = [r for r in results if r["status"] == "Retenu (en attente)"]

    best_combo = None
    best_diff = None
    if validated:
        max_size = min(coupon_max_size, len(validated))
        for size in range(1, max_size + 1):
            for combo in itertools.combinations(validated, size):
                product = 1.0
                for c in combo:
                    product *= c["selected_odds"]
                diff = abs(product - cote_cible)
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best_combo = combo

    if best_combo:
        coupon_odds = 1.0
        for c in best_combo:
            coupon_odds *= c["selected_odds"]
            for r in results:
                if r["mid"] == c["mid"]:
                    r["status"] = "Retenu"

        for r in results:
            if r["status"] == "Retenu (en attente)":
                r["status"] = "Écarté"

        st.success(f"Coupon final — cote obtenue : {coupon_odds:.2f} (cible : {cote_cible})")
        for c in best_combo:
            st.write(f"{c['mid']} — {c['label']} : {c['market']} (probabilité {c['selected_probability']:.0%}, cote {c['selected_odds']})")

        with st.spinner("Rédaction de la justification..."):
            just_raw = call_gemini(build_justification_prompt(best_combo, cote_cible, coupon_odds))
            just_clean = clean_justification_text(just_raw)
        st.write("")
        display_aerated(just_clean)
    else:
        st.error(
            f"Aucun match n'atteint le seuil de fiabilité requis ({threshold:.0%}). "
            "Aucun coupon ne peut être proposé avec les cotes fournies. Essaie avec plus de matchs ou des "
            "informations complémentaires plus précises."
        )

    with st.expander("📋 Récapitulatif complet de tous les matchs testés"):
        for r in results:
            if "selected_market" in r:
                st.write(f"{r['mid']} — {r['label']} : {r['selected_market']}, probabilité {r['selected_probability']:.0%}, cote {r['selected_odds']} — statut : {r['status']}")
            else:
                st.write(f"{r['mid']} — {r['label']} : statut — {r['status']}")

    if st.button("🔄 Nouvelle analyse"):
        for k in list(st.session_state.keys()):
            if k not in ("logged_in", "user_email"):
                del st.session_state[k]
        st.rerun()
