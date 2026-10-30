import streamlit as st

# --- Pont de compatibilite st.cache (versions recentes de Streamlit) ---
if not hasattr(st, "cache"):
    def _legacy_cache_compat(func=None, **kwargs):
        for bad_kwarg in ("allow_output_mutation", "hash_funcs", "suppress_st_warning"):
            kwargs.pop(bad_kwarg, None)
        if func is not None:
            return st.cache_resource(func)
        return st.cache_resource(**kwargs)
    st.cache = _legacy_cache_compat

import json
import math
import re
import secrets as secrets_lib
import itertools
import hashlib
import requests
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai
from streamlit_cookies_manager import EncryptedCookieManager

st.set_page_config(page_title="Oeil de l'Aigle", page_icon="🦅", layout="centered")

BACKTICK = chr(96)
STRIP_CHARS = BACKTICK + " \n"

st.markdown("""
<style>
html, body, [class*="css"], .stMarkdown, .stText, p, span, div, label {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif !important;
    font-size: 16px !important;
    font-weight: 400 !important;
    line-height: 1.6 !important;
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
</style>
""", unsafe_allow_html=True)

MAX_INFO_MATCHES = 5
MAX_POOL_SIMPLE_COMBO = 10
MAX_POOL_MULTI_AUTO = 15
MAX_MANUAL_GLOBAL = 20
DEFAULT_CONFIDENCE_THRESHOLD = 0.70
HIGH_ODDS_THRESHOLD = 15.0
HIGH_ODDS_CONFIDENCE = 0.60

PASSWORD_SALT = "oeildelaigle_salt_2024"

FOOTBALL_COMPETITIONS = {
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

MANUAL_MARKETS = {
    "Tennis": ["Vainqueur du match", "Total jeux Over/Under", "Handicap en jeux"],
    "Hockey sur glace": ["Vainqueur (incl. prolongation)", "Total buts Over/Under", "Puck line"],
    "Football americain": ["Vainqueur (Moneyline)", "Spread/Handicap", "Total points Over/Under"],
    "Football": ["1X2", "Double Chance", "BTTS", "Over/Under buts", "Total individuel equipe", "Handicap asiatique simple"],
    "Basketball": ["Victoire", "Handicap/Spread", "Over/Under points total", "Total individuel equipe"],
}

STAGE_LABELS = {
    "setup": "Configuration initiale",
    "manual_entry": "Saisie des matchs",
    "info_request": "Infos complementaires",
    "options_ready": "Options generees / cotes en attente",
    "final": "Analyse terminee",
}

PERIOD_ORDER = ["Aujourd'hui", "Hier", "Cette semaine", "Ce mois-ci", "Plus ancien"]

NO_MARKDOWN_BASE = (
    "Reponds uniquement en texte brut, sans markdown, sans asterisques, sans titres, "
    "sans listes a puces, sans blocs de code, sans JSON visible, en phrases fluides "
    "et naturelles en francais."
)

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

def ensure_default(key, default):
    if key not in st.session_state:
        st.session_state[key] = default

def normalize_probability(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    if value > 1:
        value = value / 100.0
    return max(0.0, min(1.0, value))

@st.cache_resource
def get_gspread_client():
    creds_dict = dict(st.secrets["gcp_service_account"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

def get_sheet(tab_name):
    client = get_gspread_client()
    sh = client.open_by_url(st.secrets["GOOGLE_SHEET_URL"])
    return sh.worksheet(tab_name)

def hash_password(password):
    return hashlib.sha256((password + PASSWORD_SALT).encode()).hexdigest()

def find_user_row(email):
    try:
        sheet = get_sheet("Users")
        records = sheet.get_all_values()
        for i, row in enumerate(records[1:], start=2):
            if row and row[0].strip().lower() == email.strip().lower():
                return i, row
        return None
    except Exception:
        return None

def create_user(email, password):
    sheet = get_sheet("Users")
    sheet.append_row([email.strip(), hash_password(password), datetime.now().isoformat(), "", ""])

def verify_login(email, password):
    result = find_user_row(email)
    if not result:
        return False
    _, row = result
    stored_hash = row[1] if len(row) > 1 else ""
    return stored_hash == hash_password(password)

def set_session_token(email, token):
    result = find_user_row(email)
    if not result:
        return
    row_index, _ = result
    sheet = get_sheet("Users")
    sheet.update_cell(row_index, 5, token)

def check_session_token(token):
    try:
        sheet = get_sheet("Users")
        records = sheet.get_all_values()
        for row in records[1:]:
            if len(row) >= 5 and token and row[4] == token:
                return row[0]
        return None
    except Exception:
        return None

def clear_session_token(email):
    result = find_user_row(email)
    if not result:
        return
    row_index, _ = result
    sheet = get_sheet("Users")
    sheet.update_cell(row_index, 5, "")

def classify_period(timestamp_str):
    try:
        ts = datetime.fromisoformat(timestamp_str)
    except Exception:
        return "Plus ancien"
    now = datetime.now()
    today = now.date()
    ts_date = ts.date()
    if ts_date == today:
        return "Aujourd'hui"
    elif ts_date == today - timedelta(days=1):
        return "Hier"
    elif ts_date >= today - timedelta(days=today.weekday()):
        return "Cette semaine"
    elif ts_date.year == today.year and ts_date.month == today.month:
        return "Ce mois-ci"
    else:
        return "Plus ancien"

def _prune_old_history(email, max_entries=50):
    try:
        sheet = get_sheet("History")
        records = sheet.get_all_values()
        rows_with_idx = [(i + 2, row) for i, row in enumerate(records[1:]) if row and row[0] == email]
        if len(rows_with_idx) > max_entries:
            to_delete = rows_with_idx[: len(rows_with_idx) - max_entries]
            for idx, _ in sorted(to_delete, key=lambda x: -x[0]):
                sheet.delete_rows(idx)
    except Exception:
        pass

def save_history_entry(email, stage, sport, cote_cible, session_data_dict):
    try:
        sheet = get_sheet("History")
        timestamp = datetime.now().isoformat()
        session_json = json.dumps(session_data_dict, ensure_ascii=False)
        sheet.append_row([email, timestamp, stage, sport, str(cote_cible), session_json])
        _prune_old_history(email)
    except Exception:
        pass

def get_user_history(email):
    try:
        sheet = get_sheet("History")
        records = sheet.get_all_values()
        result = []
        for i, row in enumerate(records[1:], start=2):
            if len(row) >= 6 and row[0] == email:
                try:
                    session_data = json.loads(row[5]) if row[5] else {}
                except Exception:
                    session_data = {}
                result.append({
                    "row_index": i,
                    "email": row[0],
                    "timestamp": row[1],
                    "stage": row[2],
                    "sport": row[3],
                    "cote_cible": row[4],
                    "session_data": session_data,
                })
        return result
    except Exception:
        return []

def delete_history_rows(row_indices):
    try:
        sheet = get_sheet("History")
        for idx in sorted(set(row_indices), reverse=True):
            sheet.delete_rows(idx)
        return True
    except Exception:
        return False

def delete_all_history(email):
    entries = get_user_history(email)
    row_indices = [e["row_index"] for e in entries]
    return delete_history_rows(row_indices)

def group_history_by_analysis(entries):
    by_analysis = {}
    for e in entries:
        aid = e["session_data"].get("analysis_id") or f"__no_id_{e['row_index']}"
        if aid not in by_analysis or e["timestamp"] > by_analysis[aid]["timestamp"]:
            by_analysis[aid] = e
    grouped = sorted(by_analysis.values(), key=lambda x: x["timestamp"], reverse=True)
    return grouped

def group_by_period(grouped_entries):
    periods = {p: [] for p in PERIOD_ORDER}
    for e in grouped_entries:
        period = classify_period(e["timestamp"])
        periods[period].append(e)
    return periods

def render_history_page():
    st.title("Historique de vos analyses")
    display_aerated(
        "Retrouvez ici toutes vos analyses passees, classees par periode. Cliquez sur "
        "Reprendre pour continuer une analyse interrompue, ou supprimez celles dont "
        "vous n'avez plus besoin."
    )

    entries = get_user_history(st.session_state["user_email"])

    col_a, col_b = st.columns([3, 1])
    with col_b:
        if st.button("Tout supprimer", use_container_width=True):
            st.session_state["confirm_delete_all"] = True

    if st.session_state.get("confirm_delete_all"):
        st.warning("Confirmer la suppression de TOUT l'historique ? Cette action est irreversible.")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Oui, tout supprimer"):
                delete_all_history(st.session_state["user_email"])
                st.session_state["confirm_delete_all"] = False
                st.success("Historique entierement supprime.")
                st.rerun()
        with c2:
            if st.button("Annuler"):
                st.session_state["confirm_delete_all"] = False
                st.rerun()

    if not entries:
        st.info("Aucune analyse enregistree pour le moment.")
    else:
        grouped = group_history_by_analysis(entries)
        periods = group_by_period(grouped)

        for period_index, period_name in enumerate(PERIOD_ORDER):
            period_entries = periods[period_name]
            if not period_entries:
                continue
            st.markdown(f"### {period_name}")

            if st.button(f"Supprimer le bloc {period_name}", key=f"del_block_{period_index}"):
                aids = {e["session_data"].get("analysis_id") for e in period_entries}
                rows_to_delete = []
                for raw in entries:
                    raw_aid = raw["session_data"].get("analysis_id")
                    if raw_aid is not None and raw_aid in aids:
                        rows_to_delete.append(raw["row_index"])
                    elif raw_aid is None and raw in period_entries:
                        rows_to_delete.append(raw["row_index"])
                delete_history_rows(rows_to_delete)
                st.success(f"Bloc {period_name} supprime.")
                st.rerun()

            for e in period_entries:
                sport = e["sport"] or "-"
                cote = e["cote_cible"] or "-"
                stage_label = STAGE_LABELS.get(e["stage"], e["stage"])
                try:
                    dt_display = datetime.fromisoformat(e["timestamp"]).strftime("%d/%m/%Y a %H:%M")
                except Exception:
                    dt_display = e["timestamp"]

                with st.container():
                    st.markdown(f"**{sport}** - Cote cible : {cote} - *{stage_label}*")
                    st.caption(f"Derniere activite : {dt_display}")
                    c1, c2 = st.columns([1, 1])
                    with c1:
                        if st.button("Reprendre", key=f"resume_{e['row_index']}"):
                            st.info(
                                "La reprise automatique de cette analyse sera activee a la "
                                "prochaine etape. Merci de patienter encore un peu !"
                            )
                    with c2:
                        if st.button("Supprimer", key=f"del_single_{e['row_index']}"):
                            aid = e["session_data"].get("analysis_id")
                            if aid:
                                rows_to_delete = [
                                    raw["row_index"] for raw in entries
                                    if raw["session_data"].get("analysis_id") == aid
                                ]
                            else:
                                rows_to_delete = [e["row_index"]]
                            delete_history_rows(rows_to_delete)
                            st.success("Analyse supprimee.")
                            st.rerun()
                    st.divider()

    if st.button("Retour a l'application"):
        st.session_state["view"] = "main"
        st.rerun()

def snapshot_session_data():
    return {
        "analysis_id": st.session_state.get("current_analysis_id"),
        "mode": st.session_state.get("mode"),
        "coupon_max_size": st.session_state.get("coupon_max_size"),
        "jours_fenetre": st.session_state.get("jours_fenetre"),
        "pool_limit": st.session_state.get("pool_limit"),
        "threshold": st.session_state.get("threshold"),
        "competition": st.session_state.get("data_competition"),
        "cote_cible": st.session_state.get("data_cote_cible"),
        "nb_tout": st.session_state.get("data_nb_tout"),
        "is_auto": st.session_state.get("is_auto_current"),
        "match_pool": st.session_state.get("match_pool"),
        "generated_options": st.session_state.get("generated_options"),
        "data_manual_text": st.session_state.get("data_manual_text"),
        "data_info_text": st.session_state.get("data_info_text"),
        "data_cotes_text": st.session_state.get("data_cotes_text"),
        "cotes_text_saved": st.session_state.get("data_cotes_text"),
    }

def record_progress(stage):
    try:
        save_history_entry(
            st.session_state["user_email"],
            stage,
            st.session_state.get("data_sport"),
            st.session_state.get("data_cote_cible"),
            snapshot_session_data(),
        )
    except Exception:
        pass

def fetch_football_matches(competition_code, days_window, limit):
    try:
        today = datetime.now().date()
        date_from = today.isoformat()
        date_to = (today + timedelta(days=days_window)).isoformat()
        headers = {"X-Auth-Token": st.secrets["FOOTBALL_API_KEY"]}
        if competition_code:
            url = f"https://api.football-data.org/v4/competitions/{competition_code}/matches"
        else:
            url = "https://api.football-data.org/v4/matches"
        params = {"dateFrom": date_from, "dateTo": date_to, "status": "SCHEDULED"}
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        matches = data.get("matches", [])
        pool = []
        for m in matches[:limit]:
            home = m.get("homeTeam", {}).get("name", "Equipe A")
            away = m.get("awayTeam", {}).get("name", "Equipe B")
            comp = m.get("competition", {}).get("name", "")
            date_str = m.get("utcDate", "")
            label = f"{home} vs {away} ({comp}, {date_str[:10]})"
            pool.append({"label": label, "raw": m})
        return pool
    except Exception as e:
        st.error(f"Erreur lors de la recuperation des matchs de football : {e}")
        return []

def fetch_nba_matches(days_window, limit):
    try:
        today = datetime.now().date()
        date_from = today.isoformat()
        date_to = (today + timedelta(days=days_window)).isoformat()
        headers = {"Authorization": st.secrets["BASKETBALL_API_KEY"]}
        url = "https://api.balldontlie.io/v1/games"
        params = {"start_date": date_from, "end_date": date_to, "per_page": 100}
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        games = data.get("data", [])
        pool = []
        for g in games[:limit]:
            home = g.get("home_team", {}).get("full_name", "Equipe A")
            away = g.get("visitor_team", {}).get("full_name", "Equipe B")
            date_str = g.get("date", "")
            label = f"{home} vs {away} (NBA, {date_str[:10]})"
            pool.append({"label": label, "raw": g})
        return pool
    except Exception as e:
        st.error(f"Erreur lors de la recuperation des matchs NBA : {e}")
        return []

genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])
GEMINI_MODEL = "gemini-flash-lite-latest"

def call_gemini(prompt):
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"[Erreur IA temporaire: {e}]"

def build_options_prompt(match_label, sport):
    suggestions = MANUAL_MARKETS.get(sport)
    suggestion_text = (
        f" Inspire-toi si besoin de ces types de marches habituels pour ce sport : {', '.join(suggestions)}."
        if suggestions else ""
    )
    return f"""{NO_MARKDOWN_BASE}
Tu es un analyste sportif expert en {sport}. Pour le match suivant : {match_label}, propose exactement 3 options de marche plausibles et realistes (nommees A, B et C), chacune avec une probabilite estimee, basees sur le contexte reel de ce match (forme des equipes, enjeux, historique).{suggestion_text}
Important : la probabilite doit imperativement etre exprimee sous forme d'un nombre decimal strictement compris entre 0 et 1 (par exemple 0.45 pour signifier 45 pourcent). N'exprime jamais la probabilite comme un nombre superieur a 1.
Reponds d'abord par un court paragraphe de presentation du match en 2-3 phrases.
Puis insere exactement le texte ###DATA_BLOCK### suivi d'un JSON strict de la forme:
{{"A": {{"marche": "...", "probabilite": 0.0}}, "B": {{"marche": "...", "probabilite": 0.0}}, "C": {{"marche": "...", "probabilite": 0.0}}}}
"""

def parse_options_response(text):
    try:
        parts = text.split("###DATA_BLOCK###", 1)
        narrative = parts[0].strip()
        data_part = parts[1].strip() if len(parts) > 1 else "{}"
        data_part = data_part.strip(STRIP_CHARS)
        if data_part.lower().startswith("json"):
            data_part = data_part[4:].strip()
        options = json.loads(data_part)
        for key in ["A", "B", "C"]:
            if key in options and "probabilite" in options[key]:
                options[key]["probabilite"] = normalize_probability(options[key]["probabilite"])
        return narrative, options
    except Exception:
        return text.strip(), {
            "A": {"marche": "Option A", "probabilite": 0.33},
            "B": {"marche": "Option B", "probabilite": 0.33},
            "C": {"marche": "Option C", "probabilite": 0.34},
        }

def build_reliability_prompt(matches):
    listing = "\n".join([f"- {m['label']}" for m in matches])
    return f"""{NO_MARKDOWN_BASE}
Voici une liste de matchs a venir :
{listing}
Classe ces matchs du plus previsible/fiable au moins previsible, en te basant sur la forme des equipes, les enjeux et le contexte reel.
Insere ###DATA_BLOCK### suivi d'un JSON strict de la forme {{"classement": ["libelle du match 1", "libelle du match 2", ...]}} listant TOUS les matchs du plus au moins fiable.
"""

def rank_matches_by_reliability(matches, keep_n):
    if len(matches) <= keep_n:
        return matches, 0
    prompt = build_reliability_prompt(matches)
    response = call_gemini(prompt)
    try:
        _, data_part = response.split("###DATA_BLOCK###", 1)
        data_part = data_part.strip().strip(STRIP_CHARS)
        if data_part.lower().startswith("json"):
            data_part = data_part[4:].strip()
        ranking = json.loads(data_part).get("classement", [])
        ordered = []
        for label in ranking:
            for m in matches:
                if m["label"] == label and m not in ordered:
                    ordered.append(m)
                    break
        for m in matches:
            if m not in ordered:
                ordered.append(m)
        kept = ordered[:keep_n]
        discarded_count = len(matches) - len(kept)
        return kept, discarded_count
    except Exception:
        kept = matches[:keep_n]
        return kept, len(matches) - len(kept)

def build_extra_eval_prompt(match_label, extra_market_name, sport):
    return f"""{NO_MARKDOWN_BASE}
Pour le match {match_label} en {sport}, evalue la probabilite realiste du marche suivant propose par l'utilisateur : {extra_market_name}.
Important : la probabilite doit imperativement etre un nombre decimal strictement compris entre 0 et 1 (par exemple 0.45 pour 45 pourcent), jamais un nombre superieur a 1.
Insere ###DATA_BLOCK### suivi d'un JSON strict {{"probabilite": 0.0}}.
"""

def evaluate_extra_market(match_label, extra_market_name, sport):
    prompt = build_extra_eval_prompt(match_label, extra_market_name, sport)
    response = call_gemini(prompt)
    try:
        _, data_part = response.split("###DATA_BLOCK###", 1)
        data_part = data_part.strip().strip(STRIP_CHARS)
        if data_part.lower().startswith("json"):
            data_part = data_part[4:].strip()
        raw_prob = json.loads(data_part).get("probabilite", 0.0)
        return normalize_probability(raw_prob)
    except Exception:
        return 0.0

def build_justification_prompt(selected_summary, alternatives_summary, cote_cible, produit_cotes):
    return f"""{NO_MARKDOWN_BASE}
Redige une justification en 3 a 4 phrases fluides, sans aucune liste ni accolade ni JSON, expliquant pourquoi le coupon suivant a ete retenu : {selected_summary}.
Cote cible demandee : {cote_cible}, produit des cotes obtenu : {produit_cotes:.2f}.
Compare brievement avec les autres options evaluees mais non retenues : {alternatives_summary}.
"""

def clean_justification_text(text):
    text = re.sub(r"[{}\[\]]", "", text)
    text = text.replace("###DATA_BLOCK###", "")
    text = re.sub(r"\*+", "", text)
    text = re.sub(r"#+", "", text)
    return text.strip()

def display_aerated(text):
    if not text:
        return
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    for p in paragraphs:
        st.write(p)
        st.write("")

def find_missing_mids(text, required_mids):
    present = set(re.findall(r"(M\d+)\s*:", text))
    missing = [m for m in required_mids if m not in present]
    return missing

def parse_cotes_for_match(mid, cotes_text):
    pattern = rf"{mid}\s*:\s*(.*?)(?=\nM\d+\s*:|\nEXTRA\s*:|$)"
    m = re.search(pattern, cotes_text, re.DOTALL | re.IGNORECASE)
    if not m:
        return {}
    segment = m.group(1).strip()
    cotes = {}
    for token in re.findall(r"([A-Za-z_]+)\s*=\s*([\d.,]+)", segment):
        name, val = token
        try:
            cotes[name.upper()] = float(val.replace(",", "."))
        except ValueError:
            pass
    return cotes

def parse_extra_for_match(mid, cotes_text):
    pattern = rf"{mid}.*?EXTRA\s*:\s*(.*?)(?=\nM\d+\s*:|$)"
    m = re.search(pattern, cotes_text, re.DOTALL | re.IGNORECASE)
    if not m:
        return {}
    segment = m.group(1).strip()
    extra = {}
    for part in segment.split(","):
        if "=" in part:
            name, val = part.split("=", 1)
            name = name.strip()
            try:
                extra[name] = float(val.strip().replace(",", "."))
            except ValueError:
                pass
    return extra

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
    cookies = EncryptedCookieManager(prefix="oeildelaigle_", password=st.secrets["COOKIE_PASSWORD"])
if not cookies.ready():
    st.stop()

ensure_default("logged_in", False)
ensure_default("user_email", "")

if not st.session_state["logged_in"]:
    token = cookies.get("session_token")
    if token:
        email_from_token = check_session_token(token)
        if email_from_token:
            st.session_state["logged_in"] = True
            st.session_state["user_email"] = email_from_token

if not st.session_state["logged_in"]:
    st.markdown("<h2 style='text-align:center;'>Oeil de l'Aigle</h2>", unsafe_allow_html=True)
    display_aerated("Connectez-vous ou creez un compte pour acceder a votre assistant d'analyse sportive personnel.")

    mode_login = st.radio("", ["Se connecter", "Creer un compte"], horizontal=True, label_visibility="collapsed")

    email_input = st.text_input("Compte Gmail", placeholder="votreadresse@gmail.com", key="login_email_input")
    password_input = st.text_input("Mot de passe", type="password", placeholder="Votre mot de passe", key="login_password_input")

    fields_filled = bool(email_input.strip()) and bool(password_input.strip())

    if mode_login == "Se connecter":
        if st.button("Se connecter", disabled=not fields_filled):
            if verify_login(email_input.strip(), password_input):
                new_token = secrets_lib.token_hex(16)
                set_session_token(email_input.strip(), new_token)
                cookies["session_token"] = new_token
                cookies.save()
                st.session_state["logged_in"] = True
                st.session_state["user_email"] = email_input.strip()
                st.rerun()
            else:
                st.error("Email ou mot de passe incorrect.")
        if not fields_filled:
            st.caption("Remplissez les deux champs pour continuer.")
    else:
        if st.button("Creer le compte", disabled=not fields_filled):
            if find_user_row(email_input.strip()):
                st.error("Ce compte existe deja.")
            else:
                create_user(email_input.strip(), password_input)
                st.success("Compte cree ! Vous pouvez maintenant vous connecter.")
        if not fields_filled:
            st.caption("Remplissez les deux champs pour continuer.")

    st.stop()

ensure_default("view", "main")
ensure_default("stage", "setup")
ensure_default("data_sport", "Football")
ensure_default("data_sport_prev", None)
ensure_default("data_competition", "Premier League")
ensure_default("data_cote_cible", 2.0)
ensure_default("data_nb_tout", 0)
ensure_default("data_manual_text", "")
ensure_default("data_info_text", "")
ensure_default("data_cotes_text", "")
ensure_default("match_pool", [])
ensure_default("generated_options", {})
ensure_default("current_analysis_id", None)
ensure_default("info_mids", [])
ensure_default("mode", None)
ensure_default("coupon_max_size", None)
ensure_default("jours_fenetre", None)
ensure_default("pool_limit", None)
ensure_default("threshold", DEFAULT_CONFIDENCE_THRESHOLD)
ensure_default("is_auto_current", False)
ensure_default("confirm_delete_all", False)

with st.sidebar:
    st.write(f"Connecte : {st.session_state['user_email']}")
    if st.button("Historique", use_container_width=True):
        st.session_state["view"] = "history"
        st.rerun()
    if st.button("Se deconnecter", use_container_width=True):
        clear_session_token(st.session_state["user_email"])
        if "session_token" in cookies:
            del cookies["session_token"]
            cookies.save()
        st.session_state["logged_in"] = False
        st.session_state["user_email"] = ""
        st.rerun()

if st.session_state["view"] == "history":
    render_history_page()
    st.stop()

if st.session_state["stage"] == "setup":
    st.title("Oeil de l'Aigle")
    display_aerated(
        "Bienvenue dans votre assistant d'analyse sportive. Oeil de l'Aigle scrute les matchs "
        "a venir, croise les donnees reelles disponibles et vous aide a construire un coupon "
        "coherent avec la cote que vous visez. Choisissez votre sport, votre competition et "
        "votre cote cible : l'application s'occupe du reste, en toute transparence sur ses sources."
    )

    sport_options = ["Football", "Basketball", "Tennis", "Hockey sur glace", "Football americain", "Tout"]
    sport_widget = st.selectbox(
        "Sport", sport_options,
        index=sport_options.index(st.session_state["data_sport"]) if st.session_state["data_sport"] in sport_options else 0,
        key="sport_select_widget",
    )

    competition_widget_value = st.session_state["data_competition"]
    if sport_widget == "Football":
        comp_options = list(FOOTBALL_COMPETITIONS.keys()) + ["Tout"]
        if competition_widget_value not in comp_options:
            competition_widget_value = comp_options[0]
        competition_widget = st.selectbox("Competition", comp_options, index=comp_options.index(competition_widget_value), key="competition_select_widget")
        st.success("Donnees verifiees - recuperation automatique des matchs reels.")
    elif sport_widget == "Basketball":
        comp_options = ["NBA (auto)", "Euroligue (manuel)"]
        if competition_widget_value not in comp_options:
            competition_widget_value = comp_options[0]
        competition_widget = st.selectbox("Competition", comp_options, index=comp_options.index(competition_widget_value), key="competition_select_widget")
        if competition_widget == "NBA (auto)":
            st.success("Donnees verifiees - recuperation automatique des matchs reels.")
        else:
            st.warning("Mode manuel - vous decrirez vous-meme les matchs.")
    else:
        competition_widget = None
        st.warning("Mode manuel - vous decrirez vous-meme les matchs.")

    st.session_state["data_competition"] = competition_widget

    cote_widget = st.number_input(
        "Cote cible", min_value=1.01, max_value=1500.0,
        value=float(st.session_state["data_cote_cible"]), step=0.1, key="cote_input_widget",
    )

    is_auto_preview = sport_widget == "Football" or (sport_widget == "Basketball" and competition_widget == "NBA (auto)")

    if not is_auto_preview:
        recommended = recommended_manual_matches(cote_widget)
        default_value = st.session_state["data_nb_tout"] if st.session_state["data_nb_tout"] else recommended
        nb_tout_widget = st.slider(
            "Nombre de matchs que vous allez decrire",
            min_value=1, max_value=MAX_MANUAL_GLOBAL,
            value=min(MAX_MANUAL_GLOBAL, max(1, default_value)),
            key="nb_tout_slider_widget",
        )
        display_aerated(
            f"Pour une cote cible de {cote_widget}, nous recommandons environ {recommended} "
            f"matchs afin de disposer d'assez d'options validees."
        )
        st.session_state["data_nb_tout"] = nb_tout_widget

    sport_changed = (
        st.session_state.get("data_sport_prev") is not None
        and st.session_state["data_sport_prev"] != sport_widget
    )
    has_match_data = bool(
        st.session_state["data_manual_text"].strip()
        or st.session_state["match_pool"]
        or st.session_state["data_info_text"].strip()
        or st.session_state["data_cotes_text"].strip()
    )
    if sport_changed and has_match_data:
        st.session_state["data_manual_text"] = ""
        st.session_state["data_info_text"] = ""
        st.session_state["data_cotes_text"] = ""
        st.session_state["match_pool"] = []
        st.session_state["generated_options"] = {}
        st.info("Le sport a change : les matchs, infos et cotes precedemment saisis ont ete reinitialises.")

    st.session_state["data_sport"] = sport_widget
    st.session_state["data_cote_cible"] = cote_widget
    st.session_state["data_sport_prev"] = sport_widget

    if st.button("Lancer l'analyse", use_container_width=True):
        st.session_state["current_analysis_id"] = secrets_lib.token_hex(8)

        if sport_widget == "Football":
            mode, coupon_max_size, jours, limit, threshold = determine_mode_params(cote_widget)
            competition_code = None if competition_widget == "Tout" else FOOTBALL_COMPETITIONS.get(competition_widget)
            with st.spinner("Recuperation des matchs reels en cours..."):
                pool_raw = fetch_football_matches(competition_code, jours, limit)
            pool = [{"mid": f"M{i+1}", "label": p["label"], "raw": p["raw"]} for i, p in enumerate(pool_raw)]
            st.session_state["match_pool"] = pool
            st.session_state["mode"] = mode
            st.session_state["coupon_max_size"] = coupon_max_size
            st.session_state["jours_fenetre"] = jours
            st.session_state["pool_limit"] = limit
            st.session_state["threshold"] = threshold
            st.session_state["is_auto_current"] = True

            if mode in ("simple_double", "combo_info") and pool:
                kept, discarded_count = rank_matches_by_reliability(pool, MAX_INFO_MATCHES)
                st.session_state["info_mids"] = [m["mid"] for m in kept]
                st.session_state["discarded_count"] = discarded_count
                st.session_state["stage"] = "info_request"
                record_progress("info_request")
            else:
                st.session_state["info_mids"] = []
                st.session_state["stage"] = "options_ready"
                record_progress("options_ready")
            st.rerun()

        elif sport_widget == "Basketball" and competition_widget == "NBA (auto)":
            mode, coupon_max_size, jours, limit, threshold = determine_mode_params(cote_widget)
            with st.spinner("Recuperation des matchs NBA reels en cours..."):
                pool_raw = fetch_nba_matches(jours, limit)
            pool = [{"mid": f"M{i+1}", "label": p["label"], "raw": p["raw"]} for i, p in enumerate(pool_raw)]
            st.session_state["match_pool"] = pool
            st.session_state["mode"] = mode
            st.session_state["coupon_max_size"] = coupon_max_size
            st.session_state["jours_fenetre"] = jours
            st.session_state["pool_limit"] = limit
            st.session_state["threshold"] = threshold
            st.session_state["is_auto_current"] = True

            if mode in ("simple_double", "combo_info") and pool:
                kept, discarded_count = rank_matches_by_reliability(pool, MAX_INFO_MATCHES)
                st.session_state["info_mids"] = [m["mid"] for m in kept]
                st.session_state["discarded_count"] = discarded_count
                st.session_state["stage"] = "info_request"
                record_progress("info_request")
            else:
                st.session_state["info_mids"] = []
                st.session_state["stage"] = "options_ready"
                record_progress("options_ready")
            st.rerun()

        else:
            st.session_state["threshold"] = validity_rate(cote_widget)
            st.session_state["mode"] = "manuel"
            st.session_state["coupon_max_size"] = None
            st.session_state["is_auto_current"] = False
            st.session_state["info_mids"] = []
            st.session_state["stage"] = "manual_entry"
            record_progress("manual_entry")
            st.rerun()

elif st.session_state["stage"] == "manual_entry":
    st.title("Saisie manuelle des matchs")
    display_aerated(
        f"Puisque ce sport ne beneficie pas encore d'une connexion automatique aux donnees "
        f"en direct, vous allez decrire vous-meme les matchs a analyser. Indiquez jusqu'a "
        f"{st.session_state['data_nb_tout']} rencontres, chacune precedee de son identifiant "
        f"(M1, M2, etc.), par exemple : M1: PSG vs OM."
    )
    st.warning("Mode manuel - vous etes responsable de l'exactitude des informations saisies.")

    if st.button("Retour a l'accueil"):
        st.session_state["stage"] = "setup"
        st.rerun()

    manual_widget = st.text_area(
        "Vos matchs", value=st.session_state["data_manual_text"], height=200,
        key="manual_text_widget", placeholder="M1: PSG vs OM\nM2: Real Madrid vs Barcelone",
    )
    st.session_state["data_manual_text"] = manual_widget

    required_mids = [f"M{i}" for i in range(1, st.session_state["data_nb_tout"] + 1)]
    missing = find_missing_mids(manual_widget, required_mids)

    if missing:
        st.warning(f"Il manque les identifiants suivants : {', '.join(missing)}")

    if st.button("Valider les matchs saisis", disabled=bool(missing) or not manual_widget.strip()):
        pool = []
        for mid in required_mids:
            pattern = rf"{mid}\s*:\s*(.*)"
            m = re.search(pattern, manual_widget)
            label = m.group(1).split("\n")[0].strip() if m else mid
            pool.append({"mid": mid, "label": label, "raw": None})
        st.session_state["match_pool"] = pool
        st.session_state["stage"] = "info_request"
        record_progress("info_request")
        st.rerun()

elif st.session_state["stage"] == "info_request":
    st.title("Informations complementaires")
    display_aerated(
        "Avant de generer les options d'analyse, prenez quelques minutes pour aller chercher "
        "des informations fraiches sur ces rencontres (compositions probables, blessures, "
        "forme recente, enjeux). Des sources comme Score24, Flashscore ou les sites officiels "
        "des competitions sont ideales pour ca. Collez ensuite ici ce que vous avez trouve, "
        "pour chaque match concerne."
    )

    info_mids = st.session_state.get("info_mids")
    if not info_mids:
        info_mids = [m["mid"] for m in st.session_state["match_pool"]]

    relevant_matches = [m for m in st.session_state["match_pool"] if m["mid"] in info_mids]

    st.write("Matchs concernes par cette demande d'informations :")
    for m in relevant_matches:
        st.write(f"- {m['mid']} : {m['label']}")

    if st.button("Retour a l'accueil"):
        st.session_state["stage"] = "setup"
        st.rerun()

    info_widget = st.text_area(
        "Vos informations complementaires", value=st.session_state["data_info_text"], height=220,
        key="info_text_widget",
        placeholder="M1: Compositions probables confirmees, attaquant titulaire incertain...\nM2: ...",
    )
    st.session_state["data_info_text"] = info_widget

    required_info_mids = [m["mid"] for m in relevant_matches]
    missing_info = find_missing_mids(info_widget, required_info_mids)
    if missing_info:
        st.warning(f"Merci de renseigner les informations pour : {', '.join(missing_info)}")

    if st.button("Continuer vers la generation des options", disabled=bool(missing_info) or not info_widget.strip()):
        st.session_state["stage"] = "options_ready"
        record_progress("options_ready")
        st.rerun()

elif st.session_state["stage"] == "options_ready":
    st.title("Options d'analyse")
    display_aerated(
        "Voici, pour chaque match retenu, trois options de marche plausibles accompagnees de "
        "leur probabilite estimee. Rendez-vous ensuite sur votre site de paris favori pour "
        "recuperer les cotes reelles correspondantes, puis saisissez-les ci-dessous au format demande."
    )

    match_pool = st.session_state["match_pool"]

    if not st.session_state["generated_options"]:
        options_store = {}
        with st.spinner("Generation des options en cours, cela peut prendre quelques minutes..."):
            for m in match_pool:
                prompt = build_options_prompt(m["label"], st.session_state["data_sport"])
                response = call_gemini(prompt)
                narrative, options = parse_options_response(response)
                options["_narrative"] = narrative
                options_store[m["mid"]] = options
        st.session_state["generated_options"] = options_store

    for m in match_pool:
        opts = st.session_state["generated_options"].get(m["mid"], {})
        st.subheader(f"{m['mid']} - {m['label']}")
        narrative = opts.get("_narrative", "")
        if narrative:
            display_aerated(narrative)
        for key in ["A", "B", "C"]:
            if key in opts:
                st.write(f"Option {key} : {opts[key].get('marche')} - probabilite estimee {opts[key].get('probabilite', 0)*100:.0f}%")
        st.divider()

    st.markdown("Format attendu pour chaque match : M1: A=1.85 B=1.70 C=1.90")
    st.markdown("Marche optionnel supplementaire : EXTRA: Nom_du_marche=cote,Autre_marche=cote")

    cotes_widget = st.text_area(
        "Cotes reelles", value=st.session_state["data_cotes_text"], height=220,
        key="cotes_text_widget", placeholder="M1: A=1.85 B=1.70 C=1.90\nEXTRA: Both_teams_to_score=1.95",
    )
    st.session_state["data_cotes_text"] = cotes_widget

    required_mids = [m["mid"] for m in match_pool]
    missing = find_missing_mids(cotes_widget, required_mids)
    if missing:
        st.warning(f"Il manque les cotes pour : {', '.join(missing)}")

    if st.button("Finaliser l'analyse", disabled=bool(missing) or not cotes_widget.strip()):
        st.session_state["stage"] = "final"
        record_progress("final")
        st.rerun()

elif st.session_state["stage"] == "final":
    st.title("Resultat de l'analyse")
    display_aerated(
        "Voici le fruit de l'analyse : la combinaison qui se rapproche le plus de votre cote "
        "cible, accompagnee d'une explication claire de ce choix."
    )

    match_pool = st.session_state["match_pool"]
    generated_options = st.session_state["generated_options"]
    cotes_text = st.session_state["data_cotes_text"]
    threshold = st.session_state.get("threshold", DEFAULT_CONFIDENCE_THRESHOLD)
    cote_cible = st.session_state["data_cote_cible"]
    coupon_max_size = st.session_state.get("coupon_max_size")

    evaluated = []
    for m in match_pool:
        mid = m["mid"]
        options = generated_options.get(mid, {})
        cotes = parse_cotes_for_match(mid, cotes_text)
        extra = parse_extra_for_match(mid, cotes_text)

        candidates = []
        for key in ["A", "B", "C"]:
            if key in options and key in cotes:
                candidates.append({
                    "marche": options[key].get("marche", key),
                    "probabilite": options[key].get("probabilite", 0.0),
                    "cote": cotes[key],
                })
        for name, cote_val in extra.items():
            prob = evaluate_extra_market(m["label"], name, st.session_state["data_sport"])
            candidates.append({"marche": name, "probabilite": prob, "cote": cote_val})

        if not candidates:
            evaluated.append({
                "label": m["label"], "mid": mid, "statut": "Aucune cote fournie",
                "chosen_market": None, "chosen_prob": None, "chosen_cote": None,
            })
            continue

        best_candidate = max(candidates, key=lambda c: c["probabilite"])
        validated = best_candidate["probabilite"] >= threshold
        evaluated.append({
            "label": m["label"], "mid": mid,
            "chosen_market": best_candidate["marche"],
            "chosen_prob": best_candidate["probabilite"],
            "chosen_cote": best_candidate["cote"],
            "statut": "Valide" if validated else "Rejete (sous le seuil)",
        })

    validated_matches = [e for e in evaluated if e["statut"] == "Valide"]

    if not validated_matches:
        st.warning(
            f"Aucun match n'a atteint le seuil de fiabilite requis ({int(threshold*100)}%). "
            f"Aucun coupon ne peut etre propose pour le moment. Essayez avec d'autres matchs "
            f"ou une autre cote cible."
        )
    else:
        max_size = coupon_max_size if coupon_max_size else min(len(validated_matches), 10)
        best_combo, best_diff = find_best_combination(validated_matches, cote_cible, max_size)

        if best_combo:
            produit = 1.0
            for c in best_combo:
                produit *= c["chosen_cote"]

            st.subheader("Coupon recommande")
            for c in best_combo:
                st.write(f"{c['label']} - {c['chosen_market']} (probabilite estimee {c['chosen_prob']*100:.0f}%, cote {c['chosen_cote']})")
            st.write(f"Produit des cotes : {produit:.2f} (cote cible demandee : {cote_cible})")

            selected_summary = ", ".join([f"{c['label']} ({c['chosen_market']})" for c in best_combo])
            not_selected = [e for e in validated_matches if e not in best_combo]
            alternatives_summary = (
                ", ".join([f"{e['label']} ({e['chosen_market']})" for e in not_selected])
                if not_selected else "aucune autre option validee"
            )

            justification_prompt = build_justification_prompt(selected_summary, alternatives_summary, cote_cible, produit)
            justification_raw = call_gemini(justification_prompt)
            justification = clean_justification_text(justification_raw)

            st.subheader("Justification")
            display_aerated(justification)
        else:
            st.warning("Impossible de determiner une combinaison satisfaisante.")

    with st.expander("Recapitulatif complet de tous les matchs testes"):
        for e in evaluated:
            cote_display = e["chosen_cote"] if e["chosen_cote"] is not None else "-"
            prob_display = f"{e['chosen_prob']*100:.0f}%" if e["chosen_prob"] is not None else "-"
            marche_display = e["chosen_market"] or "-"
            st.write(f"{e['label']} - Marche retenu : {marche_display} - Cote : {cote_display} - Probabilite : {prob_display} - Statut : {e['statut']}")

    if st.button("Nouvelle analyse"):
        preserved = {"logged_in": st.session_state["logged_in"], "user_email": st.session_state["user_email"]}
        st.session_state.clear()
        for k, v in preserved.items():
            st.session_state[k] = v
        st.rerun()
