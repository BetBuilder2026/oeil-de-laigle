# ============================================================
# OEIL DE L'AIGLE - Application d'analyse predictive sportive
# ============================================================

import streamlit as st
import google.generativeai as genai
import gspread
from google.oauth2.service_account import Credentials
import json
import re
import math
import itertools
import uuid
from datetime import datetime, timedelta
import requests
import hashlib

# ---- Pont de compatibilite st.cache (streamlit-cookies-manager) ----
if not hasattr(st, "cache"):
    def _legacy_cache_compat(func=None, **kwargs):
        for bad_kwarg in ("allow_output_mutation", "hash_funcs", "suppress_st_warning"):
            kwargs.pop(bad_kwarg, None)
        if func is not None:
            return st.cache_resource(func)
        return st.cache_resource(**kwargs)
    st.cache = _legacy_cache_compat

from streamlit_cookies_manager import EncryptedCookieManager

# ============================================================
# CONFIGURATION DE LA PAGE
# ============================================================

st.set_page_config(
    page_title="Œil de l'Aigle",
    page_icon="🦅",
    layout="centered",
    initial_sidebar_state="expanded",
)

# ============================================================
# CSS GLOBAL
# ============================================================

st.markdown("""
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
    color: #555;
    margin-bottom: 1.2em;
    line-height: 1.5em;
}

.badge-manual {
    background-color: #fff3cd;
    color: #856404;
    padding: 4px 12px;
    border-radius: 14px;
    font-size: 0.88em;
    font-weight: 700;
    display: inline-block;
}

.badge-verified {
    background-color: #d4edda;
    color: #155724;
    padding: 4px 12px;
    border-radius: 14px;
    font-size: 0.88em;
    font-weight: 700;
    display: inline-block;
}

.option-card {
    border: 1px solid #e0e0e0;
    border-radius: 12px;
    padding: 0.9em 1.2em;
    margin-bottom: 0.7em;
    background-color: #fafafa;
}

.step-title {
    font-size: 1.4em;
    font-weight: 800;
    margin-top: 0.3em;
    margin-bottom: 0.5em;
}
</style>
""", unsafe_allow_html=True)

# ============================================================
# CONSTANTES DE CALIBRATION
# ============================================================

MAX_INFO_MATCHES = 5
MAX_POOL_SIMPLE_COMBO = 10
MAX_POOL_MULTI_AUTO = 15
MAX_MANUAL_GLOBAL = 20
DEFAULT_CONFIDENCE_THRESHOLD = 0.70
HIGH_ODDS_THRESHOLD = 15.0
HIGH_ODDS_CONFIDENCE = 0.60

SPORT_ICONS = {
    "Football": "⚽",
    "Basketball": "🏀",
    "Tennis": "🎾",
    "Hockey sur glace": "🏒",
    "Football americain": "🏈",
    "Tout": "🌍",
}

FOOTBALL_COMPETITIONS = {
    "Toutes les competitions": "ALL",
    "Premier League": "PL",
    "Ligue 1": "FL1",
    "Liga": "PD",
    "Serie A": "SA",
    "Bundesliga": "BL1",
    "Ligue des Champions": "CL",
    "Championship": "ELC",
    "Eredivisie": "DED",
    "Primeira Liga": "PPL",
    "Brasileirao": "BSA",
}

MANUAL_SPORTS = ["Tennis", "Hockey sur glace", "Football americain", "Basketball (Euroligue)", "Tout"]

MANUAL_MARKETS = {
    "Tennis": ["Vainqueur du match", "Total jeux Over/Under", "Handicap en jeux"],
    "Hockey sur glace": ["Vainqueur (incl. prolongation)", "Total buts Over/Under", "Puck line"],
    "Football americain": ["Vainqueur (Moneyline)", "Spread/Handicap", "Total points Over/Under"],
    "Football": ["1X2", "Double Chance", "BTTS", "Over/Under buts", "Total individuel equipe", "Handicap asiatique simple"],
    "Basketball (Euroligue)": ["Victoire", "Handicap/Spread", "Over/Under points total", "Total individuel equipe"],
}

NO_MARKDOWN_BASE = (
    "Reponds uniquement en texte brut, fluide, sans markdown, sans astérisques, "
    "sans titres, sans puces, sans accolades ni JSON visible dans le texte destine a l'utilisateur. "
    "Ecris des phrases naturelles et completes."
)

ANTI_HALLUCINATION_RULE = (
    "REGLE ABSOLUE : tu dois te baser UNIQUEMENT sur les informations factuelles qui te sont "
    "explicitement fournies ci-dessus (noms des equipes, competition, date, cotes, et le texte "
    "d'informations complementaires redige par l'utilisateur). Tu n'as le droit d'utiliser aucune "
    "connaissance generale non verifiee sur les joueurs, les effectifs, les transferts, les blessures, "
    "la forme recente ou l'historique des equipes si cette information n'est pas explicitement presente "
    "dans le texte fourni par l'utilisateur. N'invente JAMAIS un fait, un nom de joueur, une statistique "
    "ou un evenement qui ne t'a pas ete communique. Si tu manques d'information sur un aspect precis, "
    "dis-le clairement au lieu de supposer ou de deviner."
)

# ============================================================
# FORMULES DE CALIBRATION DYNAMIQUE
# ============================================================

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

def normalize_probability(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    if value > 1:
        value = value / 100.0
    return max(0.0, min(1.0, value))

# ============================================================
# CONNEXION GOOGLE SHEETS
# ============================================================

@st.cache_resource
def get_gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(dict(st.secrets["gcp_service_account"]), scopes=scopes)
    return gspread.authorize(creds)

def get_spreadsheet():
    client = get_gspread_client()
    return client.open_by_url(st.secrets["GOOGLE_SHEET_URL"])

def get_users_sheet():
    return get_spreadsheet().worksheet("Users")

def get_history_sheet():
    return get_spreadsheet().worksheet("History")

def hash_password(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def find_user_row(email):
    sheet = get_users_sheet()
    records = sheet.get_all_values()
    for idx, row in enumerate(records):
        if idx == 0:
            continue
        if row and row[0].strip().lower() == email.strip().lower():
            return idx + 1, row
    return None, None

def create_user(email, password):
    sheet = get_users_sheet()
    sheet.append_row([email.strip().lower(), hash_password(password), datetime.utcnow().isoformat(), "", ""])

def authenticate_user(email, password):
    row_idx, row = find_user_row(email)
    if not row:
        return False
    stored_hash = row[1] if len(row) > 1 else ""
    return stored_hash == hash_password(password)

def set_session_token(email, token):
    row_idx, row = find_user_row(email)
    if row_idx:
        sheet = get_users_sheet()
        sheet.update_cell(row_idx, 5, token)

def check_session_token(token):
    sheet = get_users_sheet()
    records = sheet.get_all_values()
    for idx, row in enumerate(records):
        if idx == 0:
            continue
        if len(row) > 4 and row[4] == token and token:
            return row[0]
    return None

def clear_session_token(email):
    row_idx, row = find_user_row(email)
    if row_idx:
        sheet = get_users_sheet()
        sheet.update_cell(row_idx, 5, "")

# ============================================================
# FONCTIONS HISTORIQUE
# ============================================================

def new_analysis_id():
    return str(uuid.uuid4())

def log_history_entry(email, stage, sport, cote_cible, session_data_dict):
    sheet = get_history_sheet()
    sheet.append_row([
        email.strip().lower(),
        datetime.utcnow().isoformat(),
        stage,
        sport,
        str(cote_cible) if cote_cible else "",
        json.dumps(session_data_dict, ensure_ascii=False),
    ])

def get_history_entries(email):
    sheet = get_history_sheet()
    records = sheet.get_all_values()
    entries = []
    for idx, row in enumerate(records):
        if idx == 0:
            continue
        if len(row) < 6:
            continue
        if row[0].strip().lower() != email.strip().lower():
            continue
        try:
            data = json.loads(row[5]) if row[5] else {}
        except json.JSONDecodeError:
            data = {}
        entries.append({
            "row_number": idx + 1,
            "email": row[0],
            "timestamp": row[1],
            "stage": row[2],
            "sport": row[3],
            "cote_cible": row[4],
            "session_data": data,
        })
    return entries

def delete_history_rows(row_numbers):
    sheet = get_history_sheet()
    for row_number in sorted(row_numbers, reverse=True):
        sheet.delete_rows(row_number)

def snapshot_session_data():
    keys_to_save = [
        "sport", "competition", "cote_cible", "mode", "coupon_max_size",
        "jours_fenetre", "pool_limit", "threshold", "matches_pool",
        "data_info_text", "data_cotes_text", "data_extra_text",
        "relevant_matches", "options_results", "final_result", "analysis_id",
    ]
    snapshot = {}
    for key in keys_to_save:
        if key in st.session_state:
            snapshot[key] = st.session_state[key]
    return snapshot

def record_progress(stage):
    if not st.session_state.get("user_email"):
        return
    if not st.session_state.get("analysis_id"):
        st.session_state["analysis_id"] = new_analysis_id()
    try:
        log_history_entry(
            st.session_state["user_email"],
            stage,
            st.session_state.get("sport", ""),
            st.session_state.get("cote_cible", ""),
            snapshot_session_data(),
        )
    except Exception:
        pass

# ============================================================
# API SPORTIVES (DONNEES REELLES)
# ============================================================

def fetch_football_matches(competition_code, jours):
    headers = {"X-Auth-Token": st.secrets["FOOTBALL_API_KEY"]}
    date_from = datetime.utcnow().date().isoformat()
    date_to = (datetime.utcnow() + timedelta(days=jours)).date().isoformat()
    matches = []
    try:
        if competition_code == "ALL":
            codes = [c for c in FOOTBALL_COMPETITIONS.values() if c != "ALL"]
        else:
            codes = [competition_code]
        for code in codes:
            url = f"https://api.football-data.org/v4/competitions/{code}/matches"
            params = {"dateFrom": date_from, "dateTo": date_to, "status": "SCHEDULED"}
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                for m in data.get("matches", []):
                    matches.append({
                        "sport": "Football",
                        "home": m["homeTeam"]["name"],
                        "away": m["awayTeam"]["name"],
                        "date": m["utcDate"],
                        "competition": m["competition"]["name"],
                    })
    except Exception:
        pass
    return matches

def fetch_nba_matches(jours):
    headers = {"Authorization": st.secrets["BASKETBALL_API_KEY"]}
    date_from = datetime.utcnow().date().isoformat()
    date_to = (datetime.utcnow() + timedelta(days=jours)).date().isoformat()
    matches = []
    try:
        url = "https://api.balldontlie.io/v1/games"
        params = {"start_date": date_from, "end_date": date_to, "per_page": 50}
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            for g in data.get("data", []):
                matches.append({
                    "sport": "Basketball",
                    "home": g["home_team"]["full_name"],
                    "away": g["visitor_team"]["full_name"],
                    "date": g["date"],
                    "competition": "NBA",
                })
    except Exception:
        pass
    return matches

# ============================================================
# CONFIGURATION IA GEMINI
# ============================================================

def configure_gemini():
    genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])
    return genai.GenerativeModel("gemini-flash-lite-latest")

def generate_match_options(match, sport, user_info_text=""):
    model = configure_gemini()
    prompt = f"""{NO_MARKDOWN_BASE}

Tu es un analyste sportif rigoureux. Voici un match a analyser :
Sport : {sport}
Equipe A : {match.get('home', '')}
Equipe B : {match.get('away', '')}
Competition : {match.get('competition', '')}
Date : {match.get('date', '')}

Informations complementaires fournies par l'utilisateur (peuvent etre vides) :
{user_info_text if user_info_text else "Aucune information complementaire fournie."}

{ANTI_HALLUCINATION_RULE}

Propose exactement 3 options de marche plausibles et distinctes pour ce match, adaptees au sport {sport}.
Pour chacune, donne le nom du marche, le resultat precis vise, et une probabilite estimee.
La probabilite doit imperativement etre exprimee sous forme d'un nombre decimal strictement compris
entre 0 et 1 (exemple : 0.62 pour 62 pourcent). N'exprime jamais la probabilite comme un nombre
superieur a 1.

Reponds UNIQUEMENT avec un bloc JSON valide de cette forme, sans aucun texte autour :
{{"A": {{"marche": "...", "resultat": "...", "probabilite": 0.0}},
"B": {{"marche": "...", "resultat": "...", "probabilite": 0.0}},
"C": {{"marche": "...", "resultat": "...", "probabilite": 0.0}}}}
"""
    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(text)
        for key in ["A", "B", "C"]:
            if key in parsed and "probabilite" in parsed[key]:
                parsed[key]["probabilite"] = normalize_probability(parsed[key]["probabilite"])
        return parsed
    except Exception:
        return {
            "A": {"marche": "Indisponible", "resultat": "Indisponible", "probabilite": 0.0},
            "B": {"marche": "Indisponible", "resultat": "Indisponible", "probabilite": 0.0},
            "C": {"marche": "Indisponible", "resultat": "Indisponible", "probabilite": 0.0},
        }

def evaluate_extra_market(match, extra_market_name, user_info_text=""):
    model = configure_gemini()
    prompt = f"""{NO_MARKDOWN_BASE}

Sport et match : {match.get('home', '')} contre {match.get('away', '')}, competition {match.get('competition', '')}.

Informations complementaires fournies par l'utilisateur :
{user_info_text if user_info_text else "Aucune information complementaire fournie."}

{ANTI_HALLUCINATION_RULE}

Le marche supplementaire propose par l'utilisateur est : {extra_market_name}.
Estime la probabilite reelle que ce marche se realise.
La probabilite doit imperativement etre un nombre decimal strictement compris entre 0 et 1.
N'exprime jamais la probabilite comme un nombre superieur a 1.

Reponds UNIQUEMENT avec un bloc JSON valide de cette forme :
{{"probabilite": 0.0}}
"""
    try:
        response = model.generate_content(prompt)
        text = response.text.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(text)
        return normalize_probability(parsed.get("probabilite", 0.0))
    except Exception:
        return 0.0

def generate_final_justification(selected_combo, all_evaluated_matches):
    model = configure_gemini()
    selected_desc = "; ".join([
        f"{m.get('home','')} vs {m.get('away','')} ({m.get('chosen_marche','')}, cote {m.get('chosen_cote','')}, probabilite {m.get('chosen_prob',0)*100:.0f}%)"
        for m in selected_combo
    ])
    others_desc = "; ".join([
        f"{m.get('home','')} vs {m.get('away','')} (probabilite {m.get('chosen_prob',0)*100:.0f}%, statut {m.get('status','')})"
        for m in all_evaluated_matches
    ])
    prompt = f"""{NO_MARKDOWN_BASE}

{ANTI_HALLUCINATION_RULE}

Selection finale retenue : {selected_desc}
Ensemble des matchs evalues durant l'analyse : {others_desc}

Redige une justification claire et fluide de 3 a 4 phrases expliquant pourquoi cette selection a ete
retenue par rapport aux autres matchs evalues, en te basant uniquement sur les probabilites et cotes
mentionnees ci-dessus. N'utilise aucun format JSON ni accolades dans ta reponse, uniquement du texte
naturel destine a un lecteur humain.
"""
    try:
        response = model.generate_content(prompt)
        return clean_justification_text(response.text)
    except Exception:
        return "Justification indisponible pour le moment."

# ============================================================
# FONCTIONS UTILITAIRES DE PARSING
# ============================================================

def find_missing_mids(text, required_mids):
    missing = []
    found_mids = set(re.findall(r"(M\d+)\s*:", text or ""))
    for mid in required_mids:
        if mid not in found_mids:
            missing.append(mid)
    return missing

def parse_cotes_input(text):
    result = {}
    blocks = re.split(r"(M\d+)\s*:", text or "")
    for i in range(1, len(blocks), 2):
        mid = blocks[i]
        content = blocks[i + 1] if i + 1 < len(blocks) else ""
        cotes = {}
        for match in re.finditer(r"([ABCabc])\s*=\s*([\d.,]+)", content):
            letter = match.group(1).upper()
            try:
                cotes[letter] = float(match.group(2).replace(",", "."))
            except ValueError:
                pass
        result[mid] = cotes
    return result

def parse_extra_input(text):
    extra = {}
    if not text:
        return extra
    text = re.sub(r"^EXTRA\s*:\s*", "", text.strip(), flags=re.IGNORECASE)
    pairs = text.split(",")
    for pair in pairs:
        if "=" not in pair:
            continue
        name, val = pair.split("=", 1)
        name = name.strip()
        if name:
            try:
                extra[name] = float(val.strip().replace(",", "."))
            except ValueError:
                pass
    return extra

def clean_justification_text(text):
    text = re.sub(r"[{}\[\]]", "", text or "")
    text = re.sub(r"```[a-zA-Z]*", "", text)
    text = text.replace("*", "").replace("#", "")
    return text.strip()

def display_aerated(text):
    paragraphs = [p.strip() for p in (text or "").split("\n") if p.strip()]
    for p in paragraphs:
        st.markdown(p)
        st.write("")

def find_best_combination(validated_matches, cote_cible, max_size):
    best = None
    best_diff = None
    n = len(validated_matches)
    upper = min(max_size if max_size else n, n)
    for size in range(1, upper + 1):
        for combo in itertools.combinations(validated_matches, size):
            produit = 1.0
            for c in combo:
                produit *= c["chosen_cote"]
            diff = abs(produit - cote_cible)
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best = combo
    return best, best_diff
# ============================================================
# GESTION DES COOKIES DE SESSION
# ============================================================

cookies = EncryptedCookieManager(prefix="oeildelaigle_", password=st.secrets["COOKIE_PASSWORD"])
if not cookies.ready():
    st.stop()

def ensure_default(key, default):
    if key not in st.session_state:
        st.session_state[key] = default

ensure_default("logged_in", False)
ensure_default("user_email", "")

if not st.session_state["logged_in"]:
    token = cookies.get("session_token")
    if token:
        email_from_token = check_session_token(token)
        if email_from_token:
            st.session_state["logged_in"] = True
            st.session_state["user_email"] = email_from_token

# ============================================================
# PORTAIL DE CONNEXION / INSCRIPTION
# ============================================================

if not st.session_state["logged_in"]:
    st.markdown("<div class='big-title'>🦅 Œil de l'Aigle</div>", unsafe_allow_html=True)
    st.markdown(
        "<div class='subtitle'>Votre allié pour des analyses sportives precises, basees sur des "
        "donnees reelles et une intelligence rigoureuse. Connectez-vous pour acceder a vos analyses.</div>",
        unsafe_allow_html=True,
    )

    tab_login, tab_signup = st.tabs(["🔐 Se connecter", "🆕 Creer un compte"])

    with tab_login:
        email_login = st.text_input("Compte Gmail", placeholder="votre.email@gmail.com", key="login_email")
        password_login = st.text_input("Mot de passe", type="password", placeholder="Votre mot de passe", key="login_password")
        login_disabled = not (email_login.strip() and password_login.strip())
        if st.button("🔐 Se connecter", disabled=login_disabled, key="btn_login"):
            if authenticate_user(email_login, password_login):
                token = str(uuid.uuid4())
                set_session_token(email_login, token)
                cookies["session_token"] = token
                cookies.save()
                st.session_state["logged_in"] = True
                st.session_state["user_email"] = email_login.strip().lower()
                st.rerun()
            else:
                st.error("Email ou mot de passe incorrect.")
        if login_disabled:
            st.caption("Veuillez remplir les deux champs pour vous connecter.")
        with st.expander("Mot de passe oublie ?"):
            st.info("Cette fonctionnalite sera bientot disponible.")

    with tab_signup:
        email_signup = st.text_input("Compte Gmail", placeholder="votre.email@gmail.com", key="signup_email")
        password_signup = st.text_input("Mot de passe", type="password", placeholder="Choisissez un mot de passe", key="signup_password")
        signup_disabled = not (email_signup.strip() and password_signup.strip())
        if st.button("🆕 Creer mon compte", disabled=signup_disabled, key="btn_signup"):
            existing_row, _ = find_user_row(email_signup)
            if existing_row:
                st.error("Un compte existe deja avec cet email.")
            else:
                create_user(email_signup, password_signup)
                st.success("Compte cree avec succes ! Vous pouvez maintenant vous connecter.")
        if signup_disabled:
            st.caption("Veuillez remplir les deux champs pour creer un compte.")

    st.stop()

# ============================================================
# INITIALISATION DE L'ETAT DE SESSION (UTILISATEUR CONNECTE)
# ============================================================

ensure_default("page", "analysis")
ensure_default("step", "setup")
ensure_default("sport", None)
ensure_default("competition", None)
ensure_default("cote_cible", None)
ensure_default("analysis_id", None)
ensure_default("data_sport", None)
ensure_default("has_match_data", False)
ensure_default("matches_pool", [])
ensure_default("relevant_matches", [])
ensure_default("data_info_text", "")
ensure_default("data_cotes_text", "")
ensure_default("data_extra_text", "")
ensure_default("options_results", {})
ensure_default("final_result", None)
ensure_default("mode", None)
ensure_default("coupon_max_size", None)
ensure_default("jours_fenetre", None)
ensure_default("pool_limit", None)
ensure_default("threshold", None)

# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:
    st.markdown(f"### 🦅 Œil de l'Aigle")
    st.caption(f"Connecte : {st.session_state['user_email']}")
    st.write("")

    if st.button("🎯 Nouvelle analyse", use_container_width=True):
        st.session_state["page"] = "analysis"
        st.rerun()

    if st.button("📜 Historique", use_container_width=True):
        st.session_state["page"] = "history"
        st.rerun()

    st.write("")
    if st.button("🚪 Se deconnecter", use_container_width=True):
        clear_session_token(st.session_state["user_email"])
        cookies["session_token"] = ""
        cookies.save()
        st.session_state["logged_in"] = False
        st.session_state["user_email"] = ""
        st.rerun()

# ============================================================
# PAGE HISTORIQUE
# ============================================================

def period_label(timestamp_str):
    try:
        dt = datetime.fromisoformat(timestamp_str)
    except Exception:
        return "📁 Date inconnue"
    now = datetime.utcnow()
    delta_days = (now.date() - dt.date()).days
    if delta_days == 0:
        return "🟢 Aujourd'hui"
    elif delta_days == 1:
        return "🟡 Hier"
    elif delta_days <= 7:
        return "🔵 Cette semaine"
    elif delta_days <= 30:
        return "🟣 Ce mois-ci"
    else:
        return "⚪ Plus ancien"

def group_entries_by_analysis(entries):
    by_aid = {}
    for e in entries:
        aid = e["session_data"].get("analysis_id") or f"row_{e['row_number']}"
        by_aid.setdefault(aid, []).append(e)
    grouped = []
    for aid, rows in by_aid.items():
        rows_sorted = sorted(rows, key=lambda r: r["timestamp"])
        latest = rows_sorted[-1]
        grouped.append({
            "analysis_id": aid,
            "latest": latest,
            "row_numbers": [r["row_number"] for r in rows],
        })
    grouped.sort(key=lambda g: g["latest"]["timestamp"], reverse=True)
    return grouped

STAGE_LABELS = {
    "setup": "🏁 Parametrage initial",
    "manual_entry": "✍️ Saisie manuelle des matchs",
    "info_request": "🔍 Informations complementaires",
    "options_ready": "📊 Options generees",
    "final": "🚩 Analyse finalisee",
}

if st.session_state["page"] == "history":
    st.markdown("<div class='big-title'>📜 Historique de vos analyses</div>", unsafe_allow_html=True)
    st.markdown(
        "<div class='subtitle'>Retrouvez ici toutes vos analyses precedentes, classees par periode. "
        "Vous pouvez reprendre une analyse interrompue ou supprimer celles qui ne vous servent plus.</div>",
        unsafe_allow_html=True,
    )

    entries = get_history_entries(st.session_state["user_email"])

    if not entries:
        st.info("Aucune analyse enregistree pour le moment.")
    else:
        grouped = group_entries_by_analysis(entries)

        if st.button("🗑️ Tout supprimer"):
            st.session_state["confirm_delete_all"] = True

        if st.session_state.get("confirm_delete_all"):
            st.warning("Confirmez-vous la suppression de TOUT l'historique ? Cette action est irreversible.")
            col_yes, col_no = st.columns(2)
            with col_yes:
                if st.button("✅ Oui, tout supprimer"):
                    all_rows = [e["row_number"] for e in entries]
                    delete_history_rows(all_rows)
                    st.session_state["confirm_delete_all"] = False
                    st.rerun()
            with col_no:
                if st.button("❌ Annuler"):
                    st.session_state["confirm_delete_all"] = False
                    st.rerun()

        periods = {}
        for g in grouped:
            label = period_label(g["latest"]["timestamp"])
            periods.setdefault(label, []).append(g)

        period_order = ["🟢 Aujourd'hui", "🟡 Hier", "🔵 Cette semaine", "🟣 Ce mois-ci", "⚪ Plus ancien", "📁 Date inconnue"]

        for period in period_order:
            if period not in periods:
                continue
            st.markdown(f"#### {period}")

            if st.button(f"🗑️ Supprimer le bloc {period}", key=f"del_block_{period}"):
                rows_to_delete = []
                for g in periods[period]:
                    rows_to_delete.extend(g["row_numbers"])
                delete_history_rows(rows_to_delete)
                st.rerun()

            for g in periods[period]:
                latest = g["latest"]
                sd = latest["session_data"]
                sport_disp = latest["sport"] or "Sport inconnu"
                cote_disp = latest["cote_cible"] or "-"
                stage_disp = STAGE_LABELS.get(latest["stage"], latest["stage"])

                with st.container():
                    st.markdown(
                        f"<div class='option-card'><b>{SPORT_ICONS.get(sport_disp, '🏆')} {sport_disp}</b> "
                        f"— Cote cible : {cote_disp}<br>Etape : {stage_disp}<br>"
                        f"<small>{latest['timestamp']}</small></div>",
                        unsafe_allow_html=True,
                    )
                    col_resume, col_delete = st.columns(2)
                    with col_resume:
                        if st.button("▶️ Reprendre", key=f"resume_{g['analysis_id']}"):
                            st.info("La reprise complete de cette analyse sera bientot disponible.")
                    with col_delete:
                        if st.button("🗑️ Supprimer", key=f"delete_{g['analysis_id']}"):
                            delete_history_rows(g["row_numbers"])
                            st.rerun()

    st.stop()
# ============================================================
# ETAPES DE L'ANALYSE (PAGE "analysis")
# ============================================================

def reset_match_data():
    st.session_state["matches_pool"] = []
    st.session_state["relevant_matches"] = []
    st.session_state["data_info_text"] = ""
    st.session_state["data_cotes_text"] = ""
    st.session_state["data_extra_text"] = ""
    st.session_state["options_results"] = {}
    st.session_state["final_result"] = None
    st.session_state["has_match_data"] = False

def go_to_setup():
    st.session_state["step"] = "setup"

if st.session_state["page"] == "analysis":

    # --------------------------------------------------------
    # ETAPE 1 : SETUP
    # --------------------------------------------------------
    if st.session_state["step"] == "setup":
        st.markdown("<div class='big-title'>🦅 Œil de l'Aigle</div>", unsafe_allow_html=True)
        st.markdown(
            "<div class='subtitle'>Bienvenue dans votre centre d'analyse prédictive. "
            "Choisissez votre sport, votre compétition et votre cote cible : l'Aigle scrute "
            "les données réelles, croise les probabilités et vous propose la combinaison la plus "
            "fiable pour atteindre votre objectif. 🎯</div>",
            unsafe_allow_html=True,
        )

        sport_list = ["Football", "Basketball", "Tennis", "Hockey sur glace", "Football americain", "Tout"]
        default_index = sport_list.index(st.session_state["data_sport"]) if st.session_state["data_sport"] in sport_list else 0
        sport_choice = st.selectbox("🏆 Choisissez votre sport", sport_list, index=default_index, key="widget_sport")

        sport_changed = st.session_state["data_sport"] is not None and st.session_state["data_sport"] != sport_choice
        if sport_changed and st.session_state["has_match_data"]:
            reset_match_data()
        st.session_state["data_sport"] = sport_choice
        st.session_state["sport"] = sport_choice

        competition_choice = None
        is_auto_mode = False

        if sport_choice == "Football":
            competition_label = st.selectbox("⚽ Compétition", list(FOOTBALL_COMPETITIONS.keys()), key="widget_competition_foot")
            competition_choice = FOOTBALL_COMPETITIONS[competition_label]
            is_auto_mode = True

        elif sport_choice == "Basketball":
            basket_mode = st.radio("🏀 Mode", ["NBA (automatique)", "Euroligue (manuel)"], key="widget_basket_mode")
            if basket_mode == "NBA (automatique)":
                competition_choice = "NBA"
                is_auto_mode = True
            else:
                competition_choice = "Euroligue"
                is_auto_mode = False

        elif sport_choice == "Tout":
            competition_choice = "Tout"
            is_auto_mode = False

        else:
            competition_choice = sport_choice
            is_auto_mode = False

        st.session_state["competition"] = competition_choice

        cote_cible = st.number_input(
            "🎯 Cote cible visée",
            min_value=1.10, max_value=1500.0,
            value=float(st.session_state["cote_cible"]) if st.session_state["cote_cible"] else 2.00,
            step=0.05, format="%.2f",
            key="widget_cote_cible",
        )
        st.session_state["cote_cible"] = cote_cible

        if is_auto_mode:
            mode, coupon_max_size, jours, pool_limit, threshold = determine_mode_params(cote_cible)
            st.session_state["mode"] = mode
            st.session_state["coupon_max_size"] = coupon_max_size
            st.session_state["jours_fenetre"] = jours
            st.session_state["pool_limit"] = pool_limit
            st.session_state["threshold"] = threshold

            mode_desc = {
                "simple_double": "📈 Mode Simple/Double : scan rapide sur 2 jours, jusqu'à 2 sélections.",
                "combo_info": f"📊 Mode Combo : scan sur 7 jours, jusqu'à {coupon_max_size} sélections avec informations complètes.",
                "multi": "🔀 Mode Multi : scan large sur 7 jours, jusqu'à 15 matchs analysés sans infos complémentaires.",
            }
            st.info(mode_desc.get(mode, ""))

            if st.button("🔍 Lancer le scan des matchs", key="btn_launch_auto"):
                with st.spinner("Recherche des matchs réels en cours... (2 à 5 minutes)"):
                    if sport_choice == "Football":
                        pool = fetch_football_matches(competition_choice, jours)
                    else:
                        pool = fetch_nba_matches(jours)
                    pool = pool[:pool_limit]
                if not pool:
                    st.error("Aucun match trouvé pour cette période. Essayez une autre compétition ou cote.")
                else:
                    for i, m in enumerate(pool):
                        m["mid"] = f"M{i+1}"
                        m["status"] = "en_attente"
                    st.session_state["matches_pool"] = pool
                    st.session_state["has_match_data"] = True
                    if mode in ("simple_double", "combo_info"):
                        st.session_state["relevant_matches"] = pool[:MAX_INFO_MATCHES]
                    else:
                        st.session_state["relevant_matches"] = []
                    st.session_state["analysis_id"] = new_analysis_id()
                    record_progress("setup")
                    st.session_state["step"] = "info_request" if st.session_state["relevant_matches"] else "options_ready"
                    st.rerun()

        else:
            st.markdown("<span class='badge-manual'>⚠️ Mode manuel</span>", unsafe_allow_html=True)
            if sport_choice == "Tout":
                st.caption("Tous les sports sont disponibles en saisie manuelle, jusqu'à 20 événements.")

            recommended = recommended_manual_matches(cote_cible)
            nb_matches = st.slider(
                "📋 Nombre de matchs à analyser",
                min_value=1, max_value=MAX_MANUAL_GLOBAL,
                value=min(recommended, MAX_MANUAL_GLOBAL),
                key="widget_nb_matches",
            )
            st.caption(f"Nombre recommandé pour cette cote cible : {recommended} matchs (taux de fiabilité visé : {int(validity_rate(cote_cible)*100)}%).")

            if st.button("➡️ Continuer vers la saisie des matchs", key="btn_continue_manual"):
                st.session_state["manual_nb_matches"] = nb_matches
                st.session_state["step"] = "manual_entry"
                st.rerun()

    # --------------------------------------------------------
    # ETAPE 2 : SAISIE MANUELLE DES MATCHS
    # --------------------------------------------------------
    elif st.session_state["step"] == "manual_entry":
        st.markdown("<div class='step-title'>✍️ Saisie des matchs</div>", unsafe_allow_html=True)
        st.markdown("<span class='badge-manual'>⚠️ Mode manuel</span>", unsafe_allow_html=True)

        if st.button("⬅️ Retour à l'accueil", key="btn_back_manual_entry"):
            go_to_setup()
            st.rerun()

        nb_matches = st.session_state.get("manual_nb_matches", 3)
        sport_choice = st.session_state["sport"]

        matches_data = []
        for i in range(nb_matches):
            mid = f"M{i+1}"
            st.markdown(f"**{mid}**")
            col1, col2 = st.columns(2)
            with col1:
                home_key = f"data_home_{i}"
                ensure_default(home_key, "")
                home_val = st.text_input(f"Équipe A ({mid})", value=st.session_state[home_key], key=f"widget_home_{i}")
                st.session_state[home_key] = home_val
            with col2:
                away_key = f"data_away_{i}"
                ensure_default(away_key, "")
                away_val = st.text_input(f"Équipe B ({mid})", value=st.session_state[away_key], key=f"widget_away_{i}")
                st.session_state[away_key] = away_val

            if sport_choice == "Tout":
                sport_for_match_key = f"data_sport_match_{i}"
                ensure_default(sport_for_match_key, "Football")
                sport_options_all = ["Football", "Basketball", "Tennis", "Hockey sur glace", "Football americain"]
                sport_for_match = st.selectbox(
                    f"Sport ({mid})", sport_options_all,
                    index=sport_options_all.index(st.session_state[sport_for_match_key]),
                    key=f"widget_sport_match_{i}",
                )
                st.session_state[sport_for_match_key] = sport_for_match
            else:
                sport_for_match = sport_choice if sport_choice != "Basketball" else "Basketball (Euroligue)"

            matches_data.append({
                "mid": mid,
                "home": home_val,
                "away": away_val,
                "sport": sport_for_match,
                "competition": st.session_state.get("competition", ""),
                "date": "",
                "status": "en_attente",
            })
            st.write("")

        all_filled = all(m["home"].strip() and m["away"].strip() for m in matches_data)
        if not all_filled:
            st.warning("Merci de renseigner le nom des deux équipes pour chaque match avant de continuer.")

        if st.button("➡️ Continuer vers les informations complémentaires", disabled=not all_filled, key="btn_continue_to_info"):
            st.session_state["matches_pool"] = matches_data
            st.session_state["relevant_matches"] = matches_data
            st.session_state["has_match_data"] = True
            if not st.session_state.get("analysis_id"):
                st.session_state["analysis_id"] = new_analysis_id()
            record_progress("manual_entry")
            st.session_state["step"] = "info_request"
            st.rerun()

    # --------------------------------------------------------
    # ETAPE 3 : INFORMATIONS COMPLEMENTAIRES
    # --------------------------------------------------------
    elif st.session_state["step"] == "info_request":
        st.markdown("<div class='step-title'>🔍 Informations complémentaires</div>", unsafe_allow_html=True)
        st.markdown(
            "<div class='subtitle'>Avant de générer les options, partagez toute information utile "
            "(forme récente, absences connues, contexte du match...) pour chaque match concerné. "
            "Appuyez-vous idéalement sur des sources fiables comme Score24 ou les sites officiels.</div>",
            unsafe_allow_html=True,
        )

        relevant_matches = st.session_state["relevant_matches"]

        show_back_button = (
            st.session_state["sport"] in MANUAL_SPORTS
            or st.session_state["mode"] in ("simple_double", "combo_info")
        )
        if show_back_button:
            if st.button("⬅️ Retour à l'accueil", key="btn_back_info_request"):
                go_to_setup()
                st.rerun()

        st.markdown("Matchs concernés : " + ", ".join(
            [f"{m['mid']} ({m.get('home','')} vs {m.get('away','')})" for m in relevant_matches]
        ))

        ensure_default("data_info_text", "")
        info_widget = st.text_area(
            "📝 Vos informations (une mention par match, ex: 'M1: équipe A privée de son gardien titulaire')",
            value=st.session_state["data_info_text"],
            height=200,
            placeholder="M1: ...\nM2: ...\nM3: ...",
            key="widget_info_text",
        )
        st.session_state["data_info_text"] = info_widget

        required_info_mids = [m["mid"] for m in relevant_matches]
        missing_info = find_missing_mids(info_widget, required_info_mids)
        if missing_info:
            st.warning(f"Merci de renseigner les informations pour : {', '.join(missing_info)}")

        if st.button("➡️ Continuer vers la génération des options",
                      disabled=bool(missing_info) or not info_widget.strip(),
                      key="btn_continue_to_options"):
            record_progress("info_request")
            st.session_state["step"] = "options_ready"
            st.rerun()

    # --------------------------------------------------------
    # ETAPE 4 : OPTIONS GENEREES
    # --------------------------------------------------------
    elif st.session_state["step"] == "options_ready":
        st.markdown("<div class='step-title'>📊 Options et cotes réelles</div>", unsafe_allow_html=True)
        st.markdown(
            "<div class='subtitle'>L'Aigle a analysé chaque match et propose 3 options de marché "
            "plausibles. Indiquez les cotes réelles observées chez votre bookmaker pour chaque option.</div>",
            unsafe_allow_html=True,
        )

        relevant_matches = st.session_state["relevant_matches"]

        if not st.session_state["options_results"]:
            with st.spinner("Génération des options par l'Aigle..."):
                results = {}
                info_text = st.session_state.get("data_info_text", "")
                for m in relevant_matches:
                    match_info_lines = [
                        line for line in info_text.split("\n")
                        if line.strip().startswith(m["mid"] + ":")
                    ]
                    match_specific_info = "\n".join(match_info_lines)
                    results[m["mid"]] = generate_match_options(m, m.get("sport", st.session_state["sport"]), match_specific_info)
                st.session_state["options_results"] = results

        for m in relevant_matches:
            mid = m["mid"]
            opts = st.session_state["options_results"].get(mid, {})
            st.markdown(f"#### {mid} — {m.get('home','')} 🆚 {m.get('away','')}")
            badge = "<span class='badge-verified'>✅ Données vérifiées</span>" if m.get("date") else "<span class='badge-manual'>⚠️ Mode manuel</span>"
            st.markdown(badge, unsafe_allow_html=True)
            for letter in ["A", "B", "C"]:
                opt = opts.get(letter, {})
                prob_pct = opt.get("probabilite", 0) * 100
                st.markdown(
                    f"<div class='option-card'>🎯 <b>Option {letter}</b> — {opt.get('marche','')} : "
                    f"{opt.get('resultat','')} <br>📊 Probabilité estimée : {prob_pct:.0f}%</div>",
                    unsafe_allow_html=True,
                )
            st.write("")

        st.markdown("##### 💰 Saisissez les cotes réelles")
        st.caption("Format attendu : M1: A=1.85 B=1.70 C=1.90 (une ligne par match)")
        ensure_default("data_cotes_text", "")
        cotes_widget = st.text_area(
            "Cotes réelles",
            value=st.session_state["data_cotes_text"],
            height=150,
            placeholder="M1: A=1.85 B=1.70 C=1.90\nM2: A=2.10 B=3.20 C=3.50",
            key="widget_cotes_text",
        )
        st.session_state["data_cotes_text"] = cotes_widget

        st.markdown("##### 💎 Marché EXTRA (optionnel)")
        st.caption("Format : EXTRA: Marche=cote,Marche2=cote2 (sans espace ni virgule dans les noms, underscores obligatoires)")
        ensure_default("data_extra_text", "")
        extra_widget = st.text_input(
            "Marché EXTRA",
            value=st.session_state["data_extra_text"],
            placeholder="EXTRA: Plus_de_2_5_buts=1.65",
            key="widget_extra_text",
        )
        st.session_state["data_extra_text"] = extra_widget

        required_mids = [m["mid"] for m in relevant_matches]
        missing_cotes = find_missing_mids(cotes_widget, required_mids)
        if missing_cotes:
            st.warning(f"Merci de renseigner les cotes pour : {', '.join(missing_cotes)}")

        if st.button("🚩 Finaliser l'analyse", disabled=bool(missing_cotes) or not cotes_widget.strip(), key="btn_finalize"):
            cotes_parsed = parse_cotes_input(cotes_widget)
            extra_parsed = parse_extra_input(extra_widget)

            all_evaluated = []
            validated = []

            for m in st.session_state["matches_pool"]:
                mid = m["mid"]
                opts = st.session_state["options_results"].get(mid)

                if opts is None and mid in [rm["mid"] for rm in relevant_matches]:
                    continue

                if mid not in cotes_parsed:
                    m_copy = dict(m)
                    m_copy["status"] = "non evalue (hors pool restreint)"
                    m_copy["chosen_prob"] = 0.0
                    m_copy["chosen_cote"] = 0.0
                    m_copy["chosen_marche"] = "-"
                    all_evaluated.append(m_copy)
                    continue

                cotes_m = cotes_parsed.get(mid, {})
                candidates = []
                if opts:
                    for letter in ["A", "B", "C"]:
                        if letter in opts and letter in cotes_m:
                            candidates.append({
                                "letter": letter,
                                "marche": opts[letter].get("marche", ""),
                                "probabilite": opts[letter].get("probabilite", 0.0),
                                "cote": cotes_m[letter],
                            })

                extra_name = None
                for name in extra_parsed:
                    extra_name = name
                    break
                if extra_name:
                    info_text = st.session_state.get("data_info_text", "")
                    match_info_lines = [
                        line for line in info_text.split("\n")
                        if line.strip().startswith(mid + ":")
                    ]
                    match_specific_info = "\n".join(match_info_lines)
                    extra_prob = evaluate_extra_market(m, extra_name, match_specific_info)
                    candidates.append({
                        "letter": "EXTRA",
                        "marche": extra_name,
                        "probabilite": extra_prob,
                        "cote": extra_parsed[extra_name],
                    })

                if not candidates:
                    m_copy = dict(m)
                    m_copy["status"] = "aucune cote fournie"
                    m_copy["chosen_prob"] = 0.0
                    m_copy["chosen_cote"] = 0.0
                    m_copy["chosen_marche"] = "-"
                    all_evaluated.append(m_copy)
                    continue

                best_candidate = max(candidates, key=lambda c: c["probabilite"])
                m_copy = dict(m)
                m_copy["chosen_marche"] = best_candidate["marche"]
                m_copy["chosen_prob"] = best_candidate["probabilite"]
                m_copy["chosen_cote"] = best_candidate["cote"]

                threshold = st.session_state.get("threshold") or DEFAULT_CONFIDENCE_THRESHOLD
                if best_candidate["probabilite"] >= threshold:
                    m_copy["status"] = "valide"
                    validated.append(m_copy)
                else:
                    m_copy["status"] = "sous le seuil de fiabilite"

                all_evaluated.append(m_copy)

            if not validated:
                st.error(
                    f"Aucun match n'a atteint le seuil de fiabilité requis "
                    f"({int((st.session_state.get('threshold') or DEFAULT_CONFIDENCE_THRESHOLD)*100)}%). "
                    "Essayez avec d'autres matchs ou une cote cible différente."
                )
            else:
                coupon_max_size = st.session_state.get("coupon_max_size")
                best_combo, best_diff = find_best_combination(validated, st.session_state["cote_cible"], coupon_max_size)

                st.session_state["final_result"] = {
                    "combo": list(best_combo) if best_combo else [],
                    "diff": best_diff,
                    "all_evaluated": all_evaluated,
                }
                record_progress("options_ready")
                st.session_state["step"] = "final"
                st.rerun()

    # --------------------------------------------------------
    # ETAPE 5 : RESULTAT FINAL
    # --------------------------------------------------------
    elif st.session_state["step"] == "final":
        st.markdown("<div class='step-title'>🚩 Analyse finalisée</div>", unsafe_allow_html=True)

        final_result = st.session_state.get("final_result")

        if not final_result or not final_result.get("combo"):
            st.error("Aucune combinaison n'a pu être déterminée.")
        else:
            combo = final_result["combo"]
            produit_cotes = 1.0
            for c in combo:
                produit_cotes *= c["chosen_cote"]

            st.success(f"🚩 Coupon final proposé — cote totale estimée : {produit_cotes:.2f}")

            for c in combo:
                st.markdown(
                    f"<div class='option-card'>🎯 <b>{c['mid']} — {c.get('home','')} vs {c.get('away','')}</b><br>"
                    f"Marché retenu : {c.get('chosen_marche','')}<br>"
                    f"Cote : {c.get('chosen_cote','')} — 📊 Probabilité : {c.get('chosen_prob',0)*100:.0f}%</div>",
                    unsafe_allow_html=True,
                )

            combo_mids = [c["mid"] for c in combo]
            if st.session_state.get("final_justification_combo") != combo_mids:
                with st.spinner("Rédaction de la justification par l'Aigle..."):
                    justification = generate_final_justification(combo, final_result["all_evaluated"])
                st.session_state["final_justification"] = justification
                st.session_state["final_justification_combo"] = combo_mids

            st.markdown("##### 🦅 Analyse de l'Aigle")
            display_aerated(st.session_state.get("final_justification", ""))

            with st.expander("📋 Récapitulatif complet de tous les matchs testés"):
                for m in final_result["all_evaluated"]:
                    st.markdown(
                        f"- **{m.get('mid','')}** {m.get('home','')} vs {m.get('away','')} — "
                        f"Marché : {m.get('chosen_marche','-')} — "
                        f"Cote : {m.get('chosen_cote','-')} — "
                        f"Probabilité : {m.get('chosen_prob',0)*100:.0f}% — "
                        f"Statut : {m.get('status','-')}"
                    )

            record_progress("final")

        if st.button("🎯 Nouvelle analyse", key="btn_new_analysis_final"):
            keys_to_clear = [
                "step", "sport", "competition", "cote_cible", "analysis_id", "data_sport",
                "has_match_data", "matches_pool", "relevant_matches", "data_info_text",
                "data_cotes_text", "data_extra_text", "options_results", "final_result",
                "mode", "coupon_max_size", "jours_fenetre", "pool_limit", "threshold",
                "final_justification", "final_justification_combo",
            ]
            for key in keys_to_clear:
                if key in st.session_state:
                    del st.session_state[key]
            st.session_state["step"] = "setup"
            st.rerun()
