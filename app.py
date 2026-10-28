import streamlit as st
import google.generativeai as genai
import requests
import time
import re
import math
from datetime import datetime, timedelta

st.set_page_config(page_title="Œil de l'Aigle 🦅", page_icon="🦅", layout="centered")
st.title("🦅 L'Œil de l'Aigle")
st.caption("Analyse sportive basée sur données réelles et scénarios les plus probables")

st.markdown("""
<style>
h1 { font-size: 26px !important; }
h2 { font-size: 20px !important; }
h3 { font-size: 18px !important; }
p, li, div { font-size: 16px !important; line-height: 1.8 !important; }
</style>
""", unsafe_allow_html=True)

def display_aerated(text):
    st.markdown(f"<div style='white-space: pre-line; line-height: 1.9;'>{text}</div>", unsafe_allow_html=True)

genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])
FOOTBALL_HEADERS = {"X-Auth-Token": st.secrets["FOOTBALL_API_KEY"]}
BASKET_HEADERS = {"Authorization": st.secrets["BASKETBALL_API_KEY"]}

COMPETITIONS = {
    "Premier League (Angleterre)": "PL", "Ligue 1 (France)": "FL1",
    "Liga (Espagne)": "PD", "Serie A (Italie)": "SA",
    "Bundesliga (Allemagne)": "BL1", "Ligue des Champions": "CL",
    "Championship (Angleterre)": "ELC", "Eredivisie (Pays-Bas)": "DED",
    "Primeira Liga (Portugal)": "PPL", "Brasileirão (Brésil)": "BSA",
}

MAX_MATCHS_TESTES = 8

THRESHOLDS = {
    "simple_double": 0.80,
    "combo_info": 0.70,
    "multi": 0.70,
}

def get_threshold(mode):
    return THRESHOLDS.get(mode, 0.70)

def avg(results, key):
    vals = [r[key] for r in results]
    return round(sum(vals) / len(vals), 2) if vals else "N/A"

def get_standings_table(code):
    url = f"https://api.football-data.org/v4/competitions/{code}/standings"
    table = {}
    try:
        r = requests.get(url, headers=FOOTBALL_HEADERS, timeout=10)
        time.sleep(6)
        if r.status_code == 200:
            for group in r.json().get("standings", []):
                if group["type"] == "TOTAL":
                    for row in group["table"]:
                        table[row["team"]["name"]] = {
                            "position": row["position"], "points": row["points"],
                            "played": row["playedGames"], "gf": row["goalsFor"], "ga": row["goalsAgainst"]
                        }
    except Exception:
        pass
    return table

def get_football_matches(date_from, date_to, competition_label):
    matches, standings_cache = [], {}
    codes = list(COMPETITIONS.values()) if competition_label == "Tout" else [COMPETITIONS[competition_label]]
    for code in codes:
        url = f"https://api.football-data.org/v4/competitions/{code}/matches"
        params = {"dateFrom": date_from, "dateTo": date_to, "status": "SCHEDULED"}
        try:
            r = requests.get(url, headers=FOOTBALL_HEADERS, params=params, timeout=10)
            if r.status_code == 200:
                data = r.json()
                for m in data.get("matches", []):
                    matches.append({
                        "sport": "Football", "competition": data["competition"]["name"], "comp_code": code,
                        "home": m["homeTeam"]["name"], "home_id": m["homeTeam"]["id"],
                        "away": m["awayTeam"]["name"], "away_id": m["awayTeam"]["id"],
                        "date": m["utcDate"]
                    })
        except Exception:
            pass
        time.sleep(6)
        standings_cache[code] = get_standings_table(code)
    return matches, standings_cache

def get_football_form(team_id):
    url = f"https://api.football-data.org/v4/teams/{team_id}/matches"
    params = {"status": "FINISHED", "limit": 5}
    results = []
    try:
        r = requests.get(url, headers=FOOTBALL_HEADERS, params=params, timeout=10)
        time.sleep(6)
        if r.status_code == 200:
            for m in r.json().get("matches", [])[:5]:
                s = m["score"]["fullTime"]
                if s["home"] is None:
                    continue
                if m["homeTeam"]["id"] == team_id:
                    scored, conceded = s["home"], s["away"]
                else:
                    scored, conceded = s["away"], s["home"]
                results.append({
                    "display": f"{m['homeTeam']['name']} {s['home']}-{s['away']} {m['awayTeam']['name']}",
                    "scored": scored, "conceded": conceded
                })
    except Exception:
        pass
    return results

def get_basketball_matches(date_from, date_to):
    url = "https://api.balldontlie.io/v1/games"
    params = {"start_date": date_from, "end_date": date_to, "per_page": 50}
    matches = []
    try:
        r = requests.get(url, headers=BASKET_HEADERS, params=params, timeout=10)
        if r.status_code == 200:
            for g in r.json().get("data", []):
                if g["status"] != "Final":
                    matches.append({
                        "sport": "Basketball", "competition": "NBA", "comp_code": "NBA",
                        "home": g["home_team"]["full_name"], "home_id": g["home_team"]["id"],
                        "away": g["visitor_team"]["full_name"], "away_id": g["visitor_team"]["id"],
                        "date": g["date"]
                    })
    except Exception:
        pass
    return matches, {}

def get_basketball_form(team_id):
    url = "https://api.balldontlie.io/v1/games"
    params = {"team_ids[]": team_id, "per_page": 15}
    results = []
    try:
        r = requests.get(url, headers=BASKET_HEADERS, params=params, timeout=10)
        time.sleep(12)
        if r.status_code == 200:
            games = sorted(r.json().get("data", []), key=lambda x: x["date"], reverse=True)
            for g in games:
                if g["status"] == "Final" and len(results) < 5:
                    if g["home_team"]["id"] == team_id:
                        scored, conceded = g["home_team_score"], g["visitor_team_score"]
                    else:
                        scored, conceded = g["visitor_team_score"], g["home_team_score"]
                    results.append({
                        "display": f"{g['home_team']['full_name']} {g['home_team_score']}-{g['visitor_team_score']} {g['visitor_team']['full_name']}",
                        "scored": scored, "conceded": conceded
                    })
    except Exception:
        pass
    return results

def build_context_blocks(matches, sport, standings_cache):
    contexts, labels = {}, {}
    for i, m in enumerate(matches):
        mid = f"M{i+1}"
        labels[mid] = f"{m['home']} vs {m['away']} ({m['competition']})"
        if sport == "Football":
            fh = get_football_form(m["home_id"]); fa = get_football_form(m["away_id"])
            sh = standings_cache.get(m["comp_code"], {}).get(m["home"], {})
            sa = standings_cache.get(m["comp_code"], {}).get(m["away"], {})
        else:
            fh = get_basketball_form(m["home_id"]); fa = get_basketball_form(m["away_id"])
            sh, sa = {}, {}
        block = f"[{mid}] {m['home']} vs {m['away']} ({m['competition']}, {m['date']})\n"
        block += f"- {m['home']} : position {sh.get('position','N/A')} ({sh.get('points','N/A')} pts) | Moy marqués:{avg(fh,'scored')} encaissés:{avg(fh,'conceded')} | 5 derniers:{[r['display'] for r in fh]}\n"
        block += f"- {m['away']} : position {sa.get('position','N/A')} ({sa.get('points','N/A')} pts) | Moy marqués:{avg(fa,'scored')} encaissés:{avg(fa,'conceded')} | 5 derniers:{[r['display'] for r in fa]}\n"
        contexts[mid] = block
    return contexts, labels

NO_MARKDOWN_BASE = (
    "IMPORTANT : N'utilise JAMAIS de titres Markdown (pas de #, ##, ### ou ===). "
    "Utilise uniquement des emojis et du **gras** pour structurer le texte. "
    "Mets une LIGNE VIDE entre chaque paragraphe, chaque option et chaque probabilité pour aérer la lecture.\n"
)

NO_MARKDOWN_WITH_BLOCK = NO_MARKDOWN_BASE + (
    "N'écris le texte ###DATA_BLOCK### QU'UNE SEULE FOIS, à la toute fin de ta réponse, jamais ailleurs.\n"
)

PROMPT_RANKING = NO_MARKDOWN_WITH_BLOCK + """
Tu es ŒIL DE L'AIGLE. On te fournit plusieurs matchs réels avec données (classement, forme).
Classe-les du PLUS prévisible au MOINS prévisible selon l'écart de niveau/régularité.
Donne une courte phrase par match (avec une ligne vide entre chaque) puis termine OBLIGATOIREMENT par :
###DATA_BLOCK###
MID|SCORE_CONFIANCE
(score entre 0 et 1, ex: M3|0.82)
Rien après.
"""

PROMPT_MARKETS = NO_MARKDOWN_WITH_BLOCK + """
Tu es ŒIL DE L'AIGLE. Pour chaque match fourni (données réelles + infos complémentaires éventuelles), propose EXACTEMENT 3 options de marché parmi :
FOOTBALL: 1X2, Double Chance, BTTS, Over/Under buts (1.5/2.5/3.5), Total individuel équipe, Handicap asiatique simple.
BASKETBALL: Victoire, Handicap/Spread, Over/Under points total, Total individuel équipe.
Interdiction d'autres marchés (pas de corners/cartons/tirs cadrés).
Donne une probabilité réaliste (0-1) par option, basée sur les vraies données fournies.

Format par match (respecte bien les lignes vides) :

🎯 [MID] NOM DU MATCH

📊 Résumé des données prises en compte

🅰️ Option A : ...

Probabilité : XX%

🅱️ Option B : ...

Probabilité : XX%

🅲️ Option C : ...

Probabilité : XX%

Puis termine OBLIGATOIREMENT par (une seule fois, à la toute fin) :
###DATA_BLOCK###
MID|LETTRE|MARCHE|PROBA
Rien après.
"""

PROMPT_EXTRA = NO_MARKDOWN_WITH_BLOCK + """
Tu es ŒIL DE L'AIGLE. On te donne les données d'un match + une liste de marchés additionnels trouvés par l'utilisateur chez son bookmaker.
Pour CHAQUE marché, donne une probabilité réaliste (0-1) basée sur les données.
Réponds UNIQUEMENT par :
###DATA_BLOCK###
NUMERO|PROBA
(NUMERO = position dans la liste, à partir de 1)
Rien d'autre.
"""

PROMPT_JUSTIFY = NO_MARKDOWN_BASE + """
Tu es ŒIL DE L'AIGLE. On te donne les données réelles d'un match, l'option FINALEMENT retenue (avec sa probabilité et sa cote réelle), ainsi que les AUTRES options qui étaient disponibles (avec leur probabilité et leur cote).
Rédige une justification courte (3-4 phrases MAX), en texte fluide uniquement, expliquant pourquoi l'option retenue est le scénario le PLUS PROBABLE et le PLUS FIABLE par rapport aux autres options (compare les probabilités entre elles pour montrer pourquoi les autres étaient moins certaines).
INTERDICTION ABSOLUE d'afficher du code, du JSON, des accolades, des crochets, ou tout bloc technique de type ###DATA_BLOCK###. Uniquement du texte normal en français, aucun titre.
"""

model_ranking = genai.GenerativeModel(model_name="gemini-flash-lite-latest", system_instruction=PROMPT_RANKING)
model_markets = genai.GenerativeModel(model_name="gemini-flash-lite-latest", system_instruction=PROMPT_MARKETS)
model_extra = genai.GenerativeModel(model_name="gemini-flash-lite-latest", system_instruction=PROMPT_EXTRA)
model_justify = genai.GenerativeModel(model_name="gemini-flash-lite-latest", system_instruction=PROMPT_JUSTIFY)

def rank_matches(contexts):
    resp = model_ranking.generate_content("\n".join(contexts.values()))
    scores = {}
    if "###DATA_BLOCK###" in resp.text:
        _, data = resp.text.split("###DATA_BLOCK###", 1)
        for line in data.strip().split("\n"):
            p = line.strip().split("|")
            if len(p) == 2:
                try: scores[p[0].strip()] = float(p[1].strip())
                except: pass
    return scores

def generate_market_options(contexts_dict):
    resp = model_markets.generate_content("\n".join(contexts_dict.values()))
    text = resp.text
    if "###DATA_BLOCK###" in text:
        human, data = text.split("###DATA_BLOCK###", 1)
    else:
        human, data = text, ""
    options = {}
    for line in data.strip().split("\n"):
        p = line.strip().split("|")
        if len(p) == 4:
            mid, letter, name, proba = p
            try:
                options.setdefault(mid.strip(), {})[letter.strip().upper()] = {"name": name.strip(), "proba": float(proba.strip())}
            except: pass
    return human, options

def get_extra_probabilities(context_text, market_names):
    joined = "\n".join([f"{i+1}. {name}" for i, name in enumerate(market_names)])
    resp = model_extra.generate_content(f"{context_text}\n\nMarchés à évaluer :\n{joined}")
    probs = {}
    if "###DATA_BLOCK###" in resp.text:
        _, data = resp.text.split("###DATA_BLOCK###", 1)
        for line in data.strip().split("\n"):
            p = line.strip().split("|")
            if len(p) == 2:
                try: probs[int(p[0].strip())] = float(p[1].strip())
                except: pass
    return probs

def clean_justification_text(text):
    text = text.strip()
    for marker in ["###DATA_BLOCK###", "```"]:
        if marker in text:
            text = text.split(marker)[0].strip()
    return text

def get_justification(context_text, all_results, best):
    others = [r for r in all_results if r is not best]
    others_txt = "\n".join([f"- {o['market']} : probabilité {round(o['proba']*100)}%, cote {o['cote']}" for o in others])
    best_txt = f"{best['market']} : probabilité {round(best['proba']*100)}%, cote {best['cote']}"
    prompt = f"{context_text}\n\nOption retenue :\n{best_txt}\n\nAutres options disponibles :\n{others_txt if others_txt else 'Aucune autre option fournie'}"
    resp = model_justify.generate_content(prompt)
    return clean_justification_text(resp.text)

def parse_odds_line(line):
    line = line.strip()
    if not line: return None
    m = re.match(r"(M\d+)\s*:\s*(.*)", line)
    if not m: return None
    mid, rest = m.groups()
    if "EXTRA:" in rest:
        main_part, extra_part = rest.split("EXTRA:", 1)
    else:
        main_part, extra_part = rest, ""
    main_pairs = re.findall(r"([ABCabc])\s*=\s*([\d.]+)", main_part)
    main_odds = {l.upper(): float(c) for l, c in main_pairs}
    extra_pairs = re.findall(r"([A-Za-zÀ-ÿ0-9_\.\+\-]+)\s*=\s*([\d.]+)", extra_part)
    return mid.strip(), main_odds, extra_pairs

def confidence_label(p):
    if p >= 0.85:
        return "🔥 Très fiable"
    elif p >= 0.70:
        return "✅ Fiable"
    else:
        return "🔸 Fiabilité correcte"

def evaluate_match_options(mid, options_data, odds_main, extra_pairs, context_text):
    results = []
    for letter, cote in odds_main.items():
        opt = options_data.get(mid, {}).get(letter)
        if opt:
            results.append({"market": opt["name"], "proba": opt["proba"], "cote": cote})
    if extra_pairs:
        names = [n for n, _ in extra_pairs]
        probs = get_extra_probabilities(context_text, names)
        for i, (name, cote) in enumerate(extra_pairs):
            p = probs.get(i + 1)
            if p is not None:
                results.append({"market": name, "proba": p, "cote": float(cote)})
    if not results:
        return None, []
    best = max(results, key=lambda r: r["proba"])
    return best, results

if "stage" not in st.session_state:
    st.session_state.stage = "setup"

if st.session_state.stage == "setup":
    sport = st.selectbox("🏆 Choisissez le sport", ["Football", "Basketball"])
    if sport == "Basketball":
        competition = "NBA"
        st.text_input("Compétition", value="NBA", disabled=True)
    else:
        competition = st.selectbox("🏆 Choisissez la compétition", list(COMPETITIONS.keys()) + ["Tout"])
    cote_cible = st.number_input("🎯 Cote cible souhaitée", min_value=1.01, value=2.00, step=0.10)

    if st.button("🚀 Lancer le scan et l'analyse"):
        if cote_cible <= 3.00:
            mode = "simple_double"
            target_selections = 2
        else:
            estimated = max(2, round(math.log(cote_cible) / math.log(1.7)))
            if estimated <= 5:
                mode = "combo_info"
                target_selections = estimated
            else:
                mode = "multi"
                target_selections = None

        jours = 2 if mode == "simple_double" else 7
        limit = 6 if mode == "simple_double" else 15
        date_from = datetime.utcnow().strftime("%Y-%m-%d")
        date_to = (datetime.utcnow() + timedelta(days=jours)).strftime("%Y-%m-%d")

        with st.spinner(f"🦅 Scan réel en cours ({jours} jour(s), patientez...)"):
            if sport == "Football":
                matches, standings_cache = get_football_matches(date_from, date_to, competition)
            else:
                matches, standings_cache = get_basketball_matches(date_from, date_to)
            matches = matches[:limit]

            if not matches:
                st.error("Aucun match trouvé. Essayez une autre compétition ou cote.")
            else:
                contexts, labels = build_context_blocks(matches, sport, standings_cache)
                st.session_state.mode = mode
                st.session_state.cote_cible = cote_cible
                st.session_state.match_contexts = contexts
                st.session_state.match_labels = labels
                st.session_state.validated = {}
                st.session_state.rejected = set()
                st.session_state.options_data = {}
                st.session_state.extra_info_store = {}
                st.session_state.tested_count = 0
                st.session_state.limit_reached = False

                if mode in ("simple_double", "combo_info"):
                    scores = rank_matches(contexts)
                    ranked_order = sorted(contexts.keys(), key=lambda m: scores.get(m, 0), reverse=True)
                    target = min(target_selections, len(ranked_order))
                    st.session_state.target = target
                    st.session_state.ranked_order = ranked_order
                    st.session_state.active = ranked_order[:target]
                    st.session_state.tested_count = len(st.session_state.active)
                    st.session_state.reserve = ranked_order[target:]
                    st.session_state.stage = "info_request"
                else:
                    human, options = generate_market_options(contexts)
                    st.session_state.options_data = options
                    st.session_state.human_text = human
                    st.session_state.active = list(contexts.keys())
                    st.session_state.stage = "options_ready"
                st.rerun()

if st.session_state.stage == "info_request":
    threshold_pct = int(get_threshold(st.session_state.mode) * 100)
    mode_labels = {
        "simple_double": f"🎫 Mode Simple/Double (objectif : 2 sélections, seuil de fiabilité ≥ {threshold_pct}%)",
        "combo_info": f"🎫 Mode Combiné avec infos complémentaires (objectif : {st.session_state.get('target','?')} sélections, seuil ≥ {threshold_pct}%, max {MAX_MATCHS_TESTES} matchs testés)",
    }
    st.caption(mode_labels.get(st.session_state.mode, ""))
    st.subheader("📝 Infos complémentaires (forme actuelle, absences...) — optionnel mais recommandé")
    new_matches = [m for m in st.session_state.active if m not in st.session_state.extra_info_store]
    if not new_matches:
        st.caption("Aucune nouvelle info nécessaire, vous pouvez continuer.")
    for mid in new_matches:
        st.text_area(f"**{st.session_state.match_labels[mid]}**", key=f"extra_{mid}")
    if st.button("✅ Valider et analyser les marchés"):
        for mid in new_matches:
            st.session_state.extra_info_store[mid] = st.session_state.get(f"extra_{mid}", "")
        subset = {}
        for mid in st.session_state.active:
            extra = st.session_state.extra_info_store.get(mid, "")
            txt = st.session_state.match_contexts[mid]
            if extra:
                txt += f"\nInfos complémentaires utilisateur : {extra}"
            subset[mid] = txt
        with st.spinner("🦅 Analyse des marchés en cours..."):
            human, options = generate_market_options(subset)
            st.session_state.options_data.update(options)
            st.session_state.human_text = human
        st.session_state.stage = "options_ready"
        st.rerun()

if st.session_state.stage == "options_ready":
    threshold_pct = int(get_threshold(st.session_state.mode) * 100)
    mode_labels = {
        "simple_double": f"🎫 Mode Simple/Double (seuil de fiabilité ≥ {threshold_pct}%)",
        "combo_info": f"🎫 Mode Combiné avec infos complémentaires (objectif : {st.session_state.get('target','?')} sélections, seuil ≥ {threshold_pct}%, max {MAX_MATCHS_TESTES} matchs testés)",
        "multi": f"🎫 Mode Combiné base de données pure (plus de 5 sélections estimées, seuil ≥ {threshold_pct}%)",
    }
    st.caption(mode_labels.get(st.session_state.mode, ""))
    display_aerated(st.session_state.human_text)
    st.info("Format attendu :\n\nM1: A=1.85 B=1.70 C=1.90 EXTRA: Over1.5=1.25,Over3.5=2.80\n\n(EXTRA optionnel, une ligne par match affiché ci-dessus. Merci de fournir une ligne pour CHAQUE match affiché.)")
    odds_input = st.text_area("💬 Cotes réelles trouvées chez votre bookmaker")
    if st.button("✅ Analyser les cotes et finaliser"):
        with st.spinner("🦅 Analyse de fiabilité et rédaction des justifications..."):
            threshold = get_threshold(st.session_state.mode)
            processed_mids = set()
            for line in odds_input.strip().split("\n"):
                parsed = parse_odds_line(line)
                if not parsed: continue
                mid, main_odds, extra_pairs = parsed
                if mid not in st.session_state.active: continue
                processed_mids.add(mid)
                best, all_results = evaluate_match_options(mid, st.session_state.options_data, main_odds, extra_pairs, st.session_state.match_contexts[mid])
                st.session_state.active.remove(mid)
                if best is None:
                    st.session_state.rejected.add(mid)
                    continue
                if best["proba"] >= threshold:
                    justification = get_justification(st.session_state.match_contexts[mid], all_results, best)
                    st.session_state.validated[mid] = {**best, "label": st.session_state.match_labels[mid], "justification": justification}
                else:
                    st.session_state.rejected.add(mid)

            missing = [m for m in st.session_state.active if m not in processed_mids]
            if missing:
                st.warning(f"⚠️ Aucune cote détectée pour : {', '.join(missing)}. Ils ont été écartés automatiquement.")
                for m in missing:
                    st.session_state.active.remove(m)
                    st.session_state.rejected.add(m)

            if st.session_state.mode in ("simple_double", "combo_info"):
                target = st.session_state.target
                manque = target - len(st.session_state.validated)
                while manque > 0 and st.session_state.reserve and st.session_state.tested_count < MAX_MATCHS_TESTES:
                    nouveau = st.session_state.reserve.pop(0)
                    st.session_state.active.append(nouveau)
                    st.session_state.tested_count += 1
                    manque -= 1

                if len(st.session_state.validated) >= target or not st.session_state.active:
                    if len(st.session_state.validated) < target and st.session_state.tested_count >= MAX_MATCHS_TESTES:
                        st.session_state.limit_reached = True
                    st.session_state.stage = "final"
                elif st.session_state.tested_count >= MAX_MATCHS_TESTES and not st.session_state.active:
                    st.session_state.limit_reached = True
                    st.session_state.stage = "final"
                elif st.session_state.active:
                    st.session_state.stage = "info_request"
                else:
                    st.session_state.limit_reached = True
                    st.session_state.stage = "final"
            else:
                st.session_state.stage = "final"
        st.rerun()

if st.session_state.stage == "final":
    validated = st.session_state.validated
    threshold_pct = int(get_threshold(st.session_state.mode) * 100)
    if not validated:
        st.error(f"😕 Aucun marché n'a atteint le seuil de fiabilité requis (≥ {threshold_pct}%). Réessayez avec une autre compétition/cote.")
    else:
        if st.session_state.get("limit_reached"):
            st.warning(f"⚠️ Limite de {MAX_MATCHS_TESTES} matchs testés atteinte. Seulement {len(validated)} sélection(s) fiable(s) trouvée(s) sur l'objectif de {st.session_state.get('target','?')}.")

        if st.session_state.mode == "simple_double":
            vals = list(validated.values())
            cote_cible = st.session_state.cote_cible
            if len(vals) == 1:
                final_combo = vals
            else:
                single_best = max(vals, key=lambda v: v["proba"])
                combo_odds = vals[0]["cote"] * vals[1]["cote"]
                final_combo = [single_best] if abs(single_best["cote"] - cote_cible) <= abs(combo_odds - cote_cible) else vals
        else:
            vals = sorted(validated.values(), key=lambda v: v["proba"], reverse=True)
            final_combo, prod = [], 1.0
            for v in vals:
                if prod >= st.session_state.cote_cible:
                    break
                final_combo.append(v)
                prod *= v["cote"]
            if not final_combo and vals:
                final_combo = [vals[0]]

        st.markdown("**🎫 COUPON FINAL (sélection la plus fiable)**")
        st.write("")
        total_odds = 1.0
        for s in final_combo:
            total_odds *= s["cote"]
            st.markdown(f"**{s['label']}** → {s['market']}")
            st.caption(f"Cote : {s['cote']} | Probabilité : {round(s['proba']*100)}% | {confidence_label(s['proba'])}")
            st.write("")
            display_aerated(f"💡 Pourquoi ce choix : {s.get('justification', 'N/A')}")
            st.write("")
        st.markdown(f"**🎯 Cote totale : {round(total_odds,2)} (objectif : {st.session_state.cote_cible})**")
        st.warning("⚠️ Vérifiez ces cotes chez votre bookmaker avant de valider définitivement.")

    if st.button("🔄 Nouvelle analyse"):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()
