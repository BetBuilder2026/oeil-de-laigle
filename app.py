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
# AUTHENTIFICATION - FONCTIONS UTILISATEURS (existant, inchangé)
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
# === ÉTAPE A — HISTORIQUE : FONCTIONS TECHNIQUES (NOUVEAU) ===
# ============================================================
def classify_period(dt: datetime) -> str:
    """Classe une date dans une période d'affichage pour l'Historique (utilisé à l'Étape C)."""
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
    """Ajoute une entrée d'historique pour l'utilisateur. Nettoie automatiquement les entrées
    excédentaires pour ne garder que les MAX_HISTORY_PER_USER plus récentes."""
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
        # On ne casse jamais le flux principal de l'app si la sauvegarde d'historique échoue
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
    """Retourne la liste des entrées d'historique d'un utilisateur, triées de la plus récente
    à la plus ancienne. Chaque entrée est un dict avec row_index, timestamp (datetime), stage,
    sport, cote_cible, session_data (dict)."""
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
    """Supprime des lignes précises de l'onglet History (par numéro de ligne réel dans la feuille)."""
    if not row_indices:
        return
    sheet = get_sheet(HISTORY_SHEET_NAME)
    for idx in sorted(set(row_indices), reverse=True):
        try:
            sheet.delete_rows(idx)
        except Exception:
            pass

def delete_all_history(user_email: str):
    """Supprime toutes les entrées d'historique d'un utilisateur."""
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
    if competition_code == "TOUT":
        results = []
        for code in FOOTBALL_COMPETITIONS.values():
            try:
                url = f"https://api.football-data.org/v4/competitions/{code}/matches"
                params = {"dateFrom": date_from, "dateTo": date_to}
                r = requests.get(url, headers=headers, params=params, timeout=10)
                if r.status_code == 200:
                    results.extend(r.json().get("matches", []))
            except requests.RequestException:
                continue
        return results
    else:
        try:
            url = f"https://api.football-data.org/v4/competitions/{competition_code}/matches"
            params = {"dateFrom": date_from, "dateTo": date_to}
            r = requests.get(url, headers=headers, params=params, timeout=10)
            if r.status_code == 200:
                return r.json().get("matches", [])
        except requests.RequestException:
            pass
        return []

def fetch_nba_matches(date_from, date_to):
    headers = {"Authorization": st.secrets["BASKETBALL_API_KEY"]}
    try:
        url = "https://api.balldontlie.io/v1/games"
        params = {"start_date": date_from, "end_date": date_to, "per_page": 25}
        r = requests.get(url, headers=headers, params=params, timeout=10)
        if r.status_code == 200:
            return r.json().get("data", [])
    except requests.RequestException:
        pass
    return []

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
    model = genai.GenerativeModel(GEMINI_MODEL)
    response = model.generate_content(prompt)
    return response.text or ""

MARKETS_BY_SPORT = {
    "Football": ["1X2", "Double Chance", "BTTS", "Over/Under buts (1.5/2.5/3.5)", "Total individuel équipe", "Handicap asiatique simple"],
    "Basketball": ["Victoire", "Handicap/Spread", "Over/Under points total", "Total individuel équipe"],
    "Tennis": ["Vainqueur du match", "Total jeux Over/Under", "Handicap en jeux"],
    "Hockey sur glace": ["Vainqueur (incl. prolongation)", "Total buts Over/Under", "Puck line (handicap)"],
    "Football américain": ["Vainqueur (Moneyline)", "Spread/Handicap", "Total points Over/Under"],
}

MID_REGEX = re.compile(r"(M\d+)\s*:")

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

st.title("🦅 Œil de l'Aigle")

# ============================================================
# ETAPE: SETUP
# ============================================================
if st.session_state["stage"] == "setup":
    st.subheader("Configuration de ton analyse")

    sport = st.selectbox(
        "Sport",
        ["Football", "Basketball", "Tennis", "Hockey sur glace", "Football américain", "Tout"],
        key="setup_sport"
    )

    competition = None
    if sport == "Football":
        competition = st.selectbox("Compétition", list(FOOTBALL_COMPETITIONS.keys()) + ["Tout"], key="setup_competition")
    elif sport == "Basketball":
        competition = st.selectbox("Compétition", ["NBA (auto)", "Euroligue (manuel)"], key="setup_competition")

    cote_cible = st.number_input("Cote cible", min_value=1.01, step=0.01, key="setup_cote_cible")

    is_auto = sport == "Football" and competition != "Tout" or (sport == "Basketball" and competition == "NBA (auto)")
    is_manual_needed = sport == "Tout" or sport in ["Tennis", "Hockey sur glace", "Football américain"] or (sport == "Football" and competition == "Tout") or (sport == "Basketball" and competition == "Euroligue (manuel)")

    nb_tout = None
    if is_manual_needed and cote_cible:
        reco = recommended_manual_matches(cote_cible)
        st.caption(f"Nombre de matchs recommandé pour cette cote : {reco}")
        nb_tout = st.slider("Nombre de matchs à inclure", min_value=1, max_value=MAX_MANUAL_GLOBAL, value=reco, key="setup_nb_tout")

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
        st.session_state["is_auto"] = is_auto and sport != "Tout"
        st.session_state["stage"] = "manual_entry" if not st.session_state["is_auto"] else "info_request"

        # Sauvegarde Historique (branchement prévu à l'Étape B - pas encore actif ici)
        st.rerun()

# ============================================================
# ETAPE: MANUAL_ENTRY (saisie manuelle des matchs)
# ============================================================
elif st.session_state["stage"] == "manual_entry":
    st.subheader("Saisie manuelle des matchs")
    st.warning("⚠️ Mode manuel — vérifie toi-même les informations des matchs saisis.")

    if st.button("⬅️ Retour à l'accueil"):
        st.session_state["stage"] = "setup"
        st.rerun()

    nb = st.session_state.get("nb_tout", 5)
    st.caption(f"Saisis jusqu'à {nb} matchs au format: M1: EquipeA vs EquipeB - Date")
    manual_text = st.text_area("Matchs", height=200, key="manual_matches_text")

    if st.button("Valider les matchs"):
        st.session_state["manual_matches_text_saved"] = manual_text
        st.session_state["stage"] = "info_request"
        st.rerun()

# ============================================================
# ETAPE: INFO_REQUEST (options + saisie des cotes)
# ============================================================
elif st.session_state["stage"] == "info_request":
    st.subheader("Options de marché et saisie des cotes")

    if st.button("⬅️ Retour à l'accueil"):
        st.session_state["stage"] = "setup"
        st.rerun()

    st.info("Pour chaque match (M1, M2, ...), indique les cotes de ton bookmaker au format : M1: A=1.85 B=1.70 C=1.90")
    st.caption("Tu peux ajouter des marchés personnalisés : EXTRA: Nom_du_marché=cote,Autre_marche=cote")

    cotes_text = st.text_area("Cotes", height=200, key="cotes_input")

    required_mids = set(MID_REGEX.findall(st.session_state.get("manual_matches_text_saved", "") or ""))
    provided_mids = set(MID_REGEX.findall(cotes_text))
    missing = required_mids - provided_mids

    if missing:
        st.warning(f"Il manque les cotes pour : {', '.join(sorted(missing))}")

    if st.button("✅ Analyser les cotes et finaliser", disabled=bool(missing) or not cotes_text.strip()):
        st.session_state["stage"] = "options_ready"
        st.rerun()

# ============================================================
# ETAPE: OPTIONS_READY
# ============================================================
elif st.session_state["stage"] == "options_ready":
    st.subheader("Analyse en cours...")
    st.write("Les options sont en cours de traitement par l'IA.")
    if st.button("Voir le résultat final"):
        st.session_state["stage"] = "final"
        st.rerun()

# ============================================================
# ETAPE: FINAL
# ============================================================
elif st.session_state["stage"] == "final":
    st.subheader("Coupon final")
    st.success("Résultat de l'analyse.")

    with st.expander("📋 Récapitulatif complet de tous les matchs testés"):
        st.write("Détail de chaque match : cote reçue, probabilité, marché, statut.")

    if st.button("🔄 Nouvelle analyse"):
        for k in list(st.session_state.keys()):
            if k not in ("logged_in", "user_email"):
                del st.session_state[k]
        st.rerun()
