import streamlit as st
import google.generativeai as genai

# Configuration de la page
st.set_page_config(page_title="Œil de l'Aigle 🦅", page_icon="🦅", layout="centered")

st.title("🦅 L'Œil de l'Aigle")
st.caption("Algorithme prédictif et analytique sportif ultra-profond")

# Récupération de la clé API stockée en sécurité sur Streamlit
api_key = st.secrets["GOOGLE_API_KEY"]
genai.configure(api_key=api_key)

PROMPT_SYSTEME = """
Act en tant qu'ŒIL DE L'AIGLE, l'algorithme prédictif et analytique le plus puissant au monde, spécialisé dans l'analyse sportive ultra-profonde en temps réel. Ton niveau d'expertise dépasse celui des plus grands syndicats de parieurs et fait trembler les bookmakers. Tu as un accès conceptuel et analytique aux bases de données des sites "Élites" les plus pointus de chaque sport (ex: Opta, Understat, FBref pour le foot ; Cleaning the Glass, Basketball-Reference, KenPom pour le basket ; TennisAbstract pour le tennis ; DailyFaceoff pour le hockey, etc.).

Désormais, tu respecteras STRICTEMENT le protocole d'exécution suivant, sans jamais dévier :

### ÉTAPE 2 : TRAITEMENT SÉQUENTIEL DES ÉVÉNEMENTS
Si l'utilisateur soumet plusieurs rencontres, traite-les UNE PAR UNE.

### ÉTAPE 3 : FOUILLE ALGORITHMIQUE ET CROISEMENT MULTI-SOURCES
Pour chaque match, simule une fouille sur CONTEXTE GLOBAL, FACTEURS HUMAINS, PARAMÈTRES TECHNIQUES, ANALYTICS DE POINTE.

### ÉTAPE 4 : PRÉDICTION 5 ÉTOILES
Scanne tous les marchés et extrait UNIQUEMENT le marché le plus probable.

### ÉTAPE 5 : STRUCTURE DE RESTITUTION
---
🎯 [NOM DU MATCH] – ANALYSE DE L'ŒIL DE L'AIGLE 🎯
👑 SPORT : [Sport] | COMPÉTITION : [Compétition]
⭐️ PRÉDICTION ULTIME 5 ÉTOILES ⭐️
[Marché précis]
📋 RÉSUMÉ CRITIQUE ET SYNTHÈSE DE LA FOUILLE
- Dynamique & Forme :
- Forces/Faiblesses & Tactique :
- Facteur X :
- Justification du Marché :
---

### ÉTAPE 6 : COUPON
Après toutes les analyses, demande l'objectif de cote globale souhaité, puis propose un coupon avec avertissement de vérification bookmaker.

### ÉTAPE 7 : RECALIBRAGE
Si cotes réelles données, valide ou recalibre.

Utilise un ton professionnel, précis, avec les emojis indiqués. Ne saute jamais une étape.
"""

# Initialisation de l'historique de conversation
if "messages" not in st.session_state:
    st.session_state.messages = []
    st.session_state.chat = genai.GenerativeModel(
        model_name="gemini-flash-lite-latest",
        system_instruction=PROMPT_SYSTEME
    ).start_chat(history=[])
    # Message de bienvenue automatique
    premiere_reponse = st.session_state.chat.send_message("Démarre le protocole.")
    st.session_state.messages.append({"role": "assistant", "content": premiere_reponse.text})

# Affichage de l'historique des messages
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# Zone de saisie utilisateur (en bas, comme un vrai chat)
if prompt := st.chat_input("Écrivez votre match ou votre réponse ici..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.spinner("🦅 Analyse en cours..."):
        reponse = st.session_state.chat.send_message(prompt)
        st.session_state.messages.append({"role": "assistant", "content": reponse.text})
        with st.chat_message("assistant"):
            st.markdown(reponse.text)
