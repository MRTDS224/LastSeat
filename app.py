import hashlib
import os
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Any

import requests
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None

# ============================================================================
# 1. CONFIGURATION GLOBALE & MISE EN PAGE
# ============================================================================
st.set_page_config(
    layout="wide",
    page_title="Smart Flight Agent",
    page_icon="✈️",
    initial_sidebar_state="expanded",
)

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
PARTNER_API_KEY = os.getenv("PARTNER_API_KEY", "sk-partner-charter-secret-key-2026")
REQUEST_TIMEOUT = 5.0

IATA_CITIES: dict[str, str] = {
    "CMN": "Casablanca (Mohammed V)",
    "RBA": "Rabat-Salé",
    "RAK": "Marrakech-Ménara",
    "TNG": "Tanger-Ibn Battouta",
    "ORY": "Paris-Orly",
    "CDG": "Paris-Charles de Gaulle",
    "MRS": "Marseille-Provence",
    "LYS": "Lyon-Saint Exupéry",
    "LIS": "Lisbonne-Humberto Delgado",
    "BCN": "Barcelone-El Prat",
    "MAD": "Madrid-Barajas",
    "JFK": "New York-JFK",
    "LHR": "Londres-Heathrow",
}

# Styles CSS personnalisés pour les cartes d'offres, badges et Boarding Pass
st.markdown(
    """
    <style>
    .deal-badge-green {
        background-color: #dcfce7;
        color: #166534;
        padding: 4px 10px;
        border-radius: 999px;
        font-weight: 700;
        font-size: 0.82rem;
        display: inline-block;
        border: 1px solid #86efac;
    }
    .deal-badge-standard {
        background-color: #e0f2fe;
        color: #075985;
        padding: 4px 10px;
        border-radius: 999px;
        font-weight: 600;
        font-size: 0.82rem;
        display: inline-block;
        border: 1px solid #7dd3fc;
    }
    .deal-badge-soldout {
        background-color: #fee2e2;
        color: #991b1b;
        padding: 4px 10px;
        border-radius: 999px;
        font-weight: 700;
        font-size: 0.82rem;
        display: inline-block;
        border: 1px solid #fca5a5;
    }
    .agent-armed {
        background: linear-gradient(90deg, #059669 0%, #10b981 100%);
        color: white;
        padding: 8px 18px;
        border-radius: 999px;
        font-weight: 800;
        letter-spacing: 0.5px;
        display: inline-block;
        box-shadow: 0 2px 8px rgba(16, 185, 129, 0.35);
    }
    .agent-inactive {
        background-color: #475569;
        color: #f8fafc;
        padding: 8px 18px;
        border-radius: 999px;
        font-weight: 700;
        letter-spacing: 0.5px;
        display: inline-block;
    }
    .boarding-pass {
        border: 2px solid #cbd5e1;
        border-left: 10px solid #0284c7;
        border-radius: 14px;
        padding: 20px 24px;
        margin-bottom: 16px;
        background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
        color: #0f172a;
        box-shadow: 0 4px 12px rgba(15, 23, 42, 0.06);
    }
    .boarding-pass-pnr {
        font-family: monospace;
        font-size: 1.75rem;
        font-weight: 900;
        color: #0369a1;
        background: #e0f2fe;
        padding: 4px 14px;
        border-radius: 8px;
        border: 1px dashed #0284c7;
    }
    .boarding-pass-status {
        background-color: #16a34a;
        color: white;
        padding: 5px 14px;
        border-radius: 999px;
        font-weight: 800;
        font-size: 0.85rem;
        letter-spacing: 0.6px;
    }
    .boarding-pass-compensated {
        background-color: #dc2626;
        color: white;
        padding: 5px 14px;
        border-radius: 999px;
        font-weight: 800;
        font-size: 0.85rem;
        letter-spacing: 0.6px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================================
# 2. CLIENT HTTP & GESTION RÉSILIENTE DES ERREURS RÉSEAU
# ============================================================================
def api_request(
    method: str,
    endpoint: str,
    *,
    params: dict[str, Any] | None = None,
    json_payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[bool, Any]:
    """
    Exécute un appel HTTP vers le backend FastAPI sans jamais lever d'exception non gérée.
    Retourne `(True, data)` en cas de succès ou `(False, message_erreur)` en cas d'indisponibilité.
    """
    url = f"{BACKEND_URL}{endpoint}"
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)

    try:
        response = requests.request(
            method=method.upper(),
            url=url,
            params=params,
            json=json_payload,
            headers=req_headers,
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 204:
            return True, None
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            return False, f"Erreur HTTP {response.status_code} : {detail}"
        return True, response.json()
    except requests.exceptions.ConnectionError:
        return (
            False,
            (f"Backend API non joignable sur {BACKEND_URL} (port 8000). "
            "Assurez-vous que le serveur FastAPI est démarré (`uvicorn app.main:app --port 8000` ou `docker compose up`)."),
        )
    except requests.exceptions.Timeout:
        return False, f"Délai d'attente dépassé lors de la connexion à {BACKEND_URL}."
    except requests.exceptions.RequestException as exc:
        return False, f"Erreur réseau lors de l'appel à {url} : {exc}"


def fetch_dashboard_snapshot() -> dict[str, Any]:
    """Récupère l'état de santé, les offres de vols, les règles d'agents et les réservations."""
    ok_health, health_data = api_request("GET", "/health")
    if not ok_health:
        return {
            "online": False,
            "error": health_data,
            "health": {},
            "deals": [],
            "rules": [],
            "bookings": [],
        }

    ok_deals, deals_data = api_request("GET", "/api/v1/deals", params={"limit": 100})
    ok_rules, rules_data = api_request("GET", "/api/v1/rules")
    ok_bookings, bookings_data = api_request(
        "GET", "/api/v1/rules/bookings/transactions", params={"limit": 100}
    )

    return {
        "online": True,
        "error": None,
        "health": health_data if isinstance(health_data, dict) else {},
        "deals": deals_data if (ok_deals and isinstance(deals_data, list)) else [],
        "rules": rules_data if (ok_rules and isinstance(rules_data, list)) else [],
        "bookings": bookings_data if (ok_bookings and isinstance(bookings_data, list)) else [],
    }


def format_datetime_fr(iso_str: str | None) -> str:
    """Formate une date ISO-8601 en format lisible (ex: 19/10/2026 à 18:30 UTC)."""
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%d/%m/%Y à %H:%M UTC")
    except ValueError:
        return iso_str


def format_city_label(iata: str | None) -> str:
    if not iata:
        return "🌍 Partout (Wildcard)"
    city = IATA_CITIES.get(iata.upper())
    return f"{iata.upper()} ({city})" if city else iata.upper()


# ============================================================================
# 3. BARRE LATÉRALE (STATUT SYSTÈME & AUTO-REFRESH)
# ============================================================================
with st.sidebar:
    st.title("✈️ Smart Flight Agent")
    st.caption(
        "**Projet de Fin d'Études** — Plateforme événementielle d'ingestion de vols dernière minute "
        "& d'achat autonome par agent logiciel."
    )
    st.divider()

    st.subheader("⏱️ Surveillance Temps Réel")
    refresh_choice = st.selectbox(
        "Intervalle d'auto-refresh",
        options=["Désactivé", "Toutes les 3 secondes", "Toutes les 5 secondes"],
        index=2,
        help="Rafraîchit périodiquement le tableau de bord pour afficher les offres et achats autonomes en direct.",
    )

    refresh_ms = 0
    if refresh_choice == "Toutes les 3 secondes":
        refresh_ms = 3000
    elif refresh_choice == "Toutes les 5 secondes":
        refresh_ms = 5000

    if refresh_ms > 0:
        if st_autorefresh is not None:
            st_autorefresh(interval=refresh_ms, limit=None, key="smart_flight_autorefresh")
        else:
            st.caption("ℹ️ Module `streamlit-autorefresh` non détecté ; utilisez le bouton Rafraîchir.")

    st.divider()
    st.subheader("🔌 Connexion Backend")
    st.code(BACKEND_URL, language="text")

snapshot = fetch_dashboard_snapshot()

with st.sidebar:
    if snapshot["online"]:
        health = snapshot["health"]
        st.success("🟢 Backend FastAPI Connecté")
        col_s1, col_s2 = st.columns(2)
        col_s1.metric("PostgreSQL", "OK" if health.get("postgres_connected") else "KO")
        col_s2.metric("Redis Streams", "OK" if health.get("redis_connected") else "KO")
        st.caption(f"Stream : `{health.get('redis_stream', 'stream:flight_deals')}`")
    else:
        st.error("🔴 Backend Hors Ligne")

# En-tête principal
st.title("✈️ Smart Last-Minute Flight Booking Agent")
st.markdown(
    "Tableau de bord de démonstration temps réel : **Ingestion Multi-Sources (GDS & Voyagistes)** • "
    "**Redis Streams** • **Verrous Distribués (`SET NX EX`)** • **Achat Autonome & Saga Pattern**"
)

if not snapshot["online"]:
    st.error(f"⚠️ **{snapshot['error']}**")
    st.info(
        "💡 **Astuce pour la démo :** Démarrez l'infrastructure avec `docker compose up -d` "
        "ou lancez l'API avec `uv run uvicorn app.main:app --reload --port 8000`, puis cliquez sur **Rafraîchir**."
    )
    if st.button("🔄 Réessayer la connexion au Backend", type="primary"):
        st.rerun()
    st.stop()

deals_list: list[dict[str, Any]] = snapshot["deals"]
rules_list: list[dict[str, Any]] = snapshot["rules"]
bookings_list: list[dict[str, Any]] = snapshot["bookings"]

# Lookup rapide des deals par deal_id pour enrichir les Boarding Passes
deals_by_id: dict[str, dict[str, Any]] = {d["deal_id"]: d for d in deals_list}
rules_by_id: dict[str, dict[str, Any]] = {str(r["id"]): r for r in rules_list}

# ============================================================================
# 4. ARCHITECTURE EN 4 ONGLETS
# ============================================================================
tab_feed, tab_agent, tab_simulator, tab_bookings = st.tabs(
    [
        "📡 1. Live Feed & Détection des Vols",
        "🤖 2. Configuration de l'Agent (Voyageur)",
        "🧪 3. Simulateur Partenaire (Démo Jury)",
        "🎫 4. Mes Réservations & Billets Émis",
    ]
)

# ----------------------------------------------------------------------------
# ONGLET 1 : LIVE FEED & DÉTECTION DES VOLS
# ----------------------------------------------------------------------------
with tab_feed:
    today_utc = datetime.now(UTC).date()
    scanned_today = sum(
        1
        for d in deals_list
        if d.get("created_at", "").startswith(today_utc.isoformat())
    ) or len(deals_list)

    active_cheap_deals = [
        d
        for d in deals_list
        if d.get("status") == "ACTIVE"
        and int(d.get("seats_available", 0)) > 0
        and float(d.get("price", 9999)) < 60.0
    ]
    confirmed_bookings = [b for b in bookings_list if b.get("status") == "CONFIRMED"]

    # Bandeau d'indicateurs clés (st.metric)
    kpi_col1, kpi_col2, kpi_col3, kpi_col4 = st.columns(4)
    with kpi_col1, st.container(border=True):
        st.metric(
            label="📡 Vols scannés aujourd'hui",
            value=scanned_today,
            delta=f"{len(deals_list)} offres en base",
        )
    with kpi_col2, st.container(border=True):
        st.metric(
            label="🔥 Deals actifs (< 60 €)",
            value=len(active_cheap_deals),
            delta="Cibles prioritaires Agent",
        )
    with kpi_col3, st.container(border=True):
        st.metric(
            label="✅ Réservations confirmées",
            value=len(confirmed_bookings),
            delta="Émission PNR automatique",
        )
    with kpi_col4, st.container(border=True):
        armed_rules_count = sum(1 for r in rules_list if r.get("auto_purchase_enabled"))
        st.metric(
            label="🤖 Agents autonomes armés",
            value=armed_rules_count,
            delta=f"sur {len(rules_list)} configurés",
        )

    # Barre d'actions et de filtres du Live Feed
    ctrl_col1, ctrl_col2, ctrl_col3, ctrl_col4 = st.columns([1.4, 1.6, 2.0, 2.0])
    with ctrl_col1:
        if st.button("🔄 Rafraîchir les offres", use_container_width=True, type="primary"):
            st.rerun()
    with ctrl_col2:
        if st.button("⚡ Simuler un Poll GDS", use_container_width=True):
            ok_poll, poll_res = api_request("POST", "/api/v1/deals/poll-now")
            if ok_poll:
                st.toast(
                    f"Nouveau vol GDS détecté : {poll_res.get('deal_id')} !",
                    icon="✈️",
                )
                time.sleep(0.3)
                st.rerun()
            else:
                st.error(poll_res)
    with ctrl_col3:
        filter_status = st.selectbox(
            "Filtrer par statut",
            options=["Tous les vols", "ACTIVE uniquement", "Deals Flash (< 60 €)", "SOLD_OUT"],
            label_visibility="collapsed",
        )
    with ctrl_col4:
        view_mode = st.radio(
            "Affichage",
            options=["Cartes Visuelles", "Tableau Dynamique"],
            horizontal=True,
            label_visibility="collapsed",
        )

    # Filtrage des offres
    filtered_deals = deals_list
    if filter_status == "ACTIVE uniquement":
        filtered_deals = [d for d in deals_list if d.get("status") == "ACTIVE"]
    elif filter_status == "Deals Flash (< 60 €)":
        filtered_deals = active_cheap_deals
    elif filter_status == "SOLD_OUT":
        filtered_deals = [d for d in deals_list if d.get("status") == "SOLD_OUT"]

    if not filtered_deals:
        st.info(
            "Aucune offre de vol ne correspond au filtre actuel. "
            "Cliquez sur **⚡ Simuler un Poll GDS** ou utilisez l'onglet **Simulateur Partenaire**."
        )
    elif view_mode == "Tableau Dynamique":
        table_rows = []
        for d in filtered_deals:
            price_val = float(d.get("price", 0))
            table_rows.append(
                {
                    "ID Deal": d.get("deal_id"),
                    "Vol": f"{d.get('flight_number')} ({d.get('airline_code')})",
                    "Itinéraire": f"{d.get('origin')} ➔ {d.get('destination')}",
                    "Départ (UTC)": format_datetime_fr(d.get("departure_at")),
                    "Sièges restants": int(d.get("seats_available", 0)),
                    "Prix (€)": f"{price_val:.2f} {d.get('currency', 'EUR')}",
                    "Réduction": "🟢 OUI (< 60 €)" if price_val < 60.0 else "Standard",
                    "Source": d.get("source"),
                    "Statut": d.get("status"),
                }
            )
        st.dataframe(table_rows, use_container_width=True, hide_index=True)
    else:
        # Affichage sous forme de cartes élégantes (2 colonnes)
        card_cols = st.columns(2)
        for idx, deal in enumerate(filtered_deals):
            price = float(deal.get("price", 0.0))
            seats = int(deal.get("seats_available", 0))
            status_str = deal.get("status", "ACTIVE")
            raw_meta = deal.get("raw_metadata") or {}
            ref_price = float(raw_meta.get("reference_price", max(price * 2.2, 140.0)))
            discount_pct = max(0, round((1 - (price / ref_price)) * 100))

            with card_cols[idx % 2], st.container(border=True):
                top_left, top_right = st.columns([2.2, 1.3])
                with top_left:
                    st.markdown(
                        f"### ✈️ Vol `{deal.get('flight_number')}` — "
                        f"**{deal.get('origin')} ➔ {deal.get('destination')}**"
                    )
                    st.caption(
                        f"{format_city_label(deal.get('origin'))} vers "
                        f"{format_city_label(deal.get('destination'))} • "
                        f"Source : `{deal.get('source')}`"
                    )
                with top_right:
                    if status_str == "SOLD_OUT" or seats == 0:
                        st.markdown(
                            '<div style="text-align:right;"><span class="deal-badge-soldout">'
                            "COMPLET / ACHETÉ PAR AGENT</span></div>",
                            unsafe_allow_html=True,
                        )
                    elif price <= 60.0 or discount_pct >= 50:
                        st.markdown(
                            f'<div style="text-align:right;"><span class="deal-badge-green">'
                            f"🔥 -{discount_pct}% • FLASH DEAL</span></div>",
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            f'<div style="text-align:right;"><span class="deal-badge-standard">'
                            f"-{discount_pct}% • DERNIÈRE MINUTE</span></div>",
                            unsafe_allow_html=True,
                        )

                info_c1, info_c2, info_c3 = st.columns([1.6, 1.2, 1.2])
                with info_c1:
                    st.markdown(
                        f"🕒 **Départ :**\n`{format_datetime_fr(deal.get('departure_at'))}`"
                    )
                with info_c2:
                    seat_icon = "🔴" if seats == 0 else ("🟠" if seats <= 2 else "🟢")
                    st.markdown(f"💺 **Sièges restants :**\n{seat_icon} **{seats} siège(s)**")
                with info_c3:
                    st.markdown(
                        f"💶 **Prix cassé :**\n"
                        f"<span style='font-size:1.35rem;font-weight:800;color:#16a34a;'>"
                        f"{price:.2f} {deal.get('currency', 'EUR')}</span> "
                        f"<s style='color:#94a3b8;font-size:0.85rem;'>{ref_price:.0f} €</s>",
                        unsafe_allow_html=True,
                    )


# ----------------------------------------------------------------------------
# ONGLET 2 : CONFIGURATION DE L'AGENT D'ACHAT (VOYAGEUR)
# ----------------------------------------------------------------------------
with tab_agent:
    any_armed = any(r.get("auto_purchase_enabled") for r in rules_list)
    header_col1, header_col2 = st.columns([2.5, 1.5])
    with header_col1:
        st.subheader("🤖 Paramétrage de l'Agent d'Achat Autonome")
        st.write(
            "Configurez vos critères de voyage et votre plafond budgétaire. Dès qu'une offre "
            "correspondante apparaît sur Redis Streams, l'agent verrouille le siège et émet votre billet."
        )
    with header_col2:
        if any_armed:
            st.markdown(
                '<div style="text-align:right;margin-top:12px;">'
                '<span class="agent-armed">🟢 STATUT AGENT : ARMÉ & EN ATTENTE</span></div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<div style="text-align:right;margin-top:12px;">'
                '<span class="agent-inactive">⚪ STATUT AGENT : INACTIF</span></div>',
                unsafe_allow_html=True,
            )

    st.divider()

    form_col, rules_col = st.columns([1.35, 1.65], gap="large")

    with form_col:
        with st.container(border=True):
            st.markdown("#### ➕ Créer & Armer une Règle d'Achat")

            wildcard_dest = st.checkbox(
                "🌍 Destination : **N'importe où / Partout (Wildcard)**",
                value=False,
                help="Si coché, l'agent achètera tout billet partant de votre aéroport d'origine sous le budget fixé.",
            )

            with st.form("agent_rule_form", clear_on_submit=False):
                f_c1, f_c2 = st.columns(2)
                with f_c1:
                    user_id_input = st.text_input(
                        "Identifiant Voyageur",
                        value="usr_voyageur_demo",
                    )
                    first_name_input = st.text_input("Prénom du passager", value="Yassine")
                with f_c2:
                    email_input = st.text_input(
                        "Email de notification", value="yassine.demo@example.com"
                    )
                    last_name_input = st.text_input("Nom du passager", value="El Amrani")

                r_c1, r_c2 = st.columns(2)
                with r_c1:
                    origin_input = st.selectbox(
                        "Aéroport de départ (Code IATA)",
                        options=["CMN", "RBA", "ORY", "CDG", "RAK", "TNG", "MRS", "LYS"],
                        index=0,
                        format_func=format_city_label,
                    )
                with r_c2:
                    if wildcard_dest:
                        st.text_input(
                            "Destination cible",
                            value="🌍 N'importe où / Partout (Wildcard)",
                            disabled=True,
                        )
                        destination_input = None
                    else:
                        destination_input = st.selectbox(
                            "Destination cible (Code IATA)",
                            options=["ORY", "CDG", "LIS", "BCN", "MAD", "JFK", "CMN", "RBA"],
                            index=0,
                            format_func=format_city_label,
                        )

                b_c1, b_c2 = st.columns([2, 1])
                with b_c1:
                    max_price_input = st.slider(
                        "💶 Budget maximum (EUR)",
                        min_value=15.0,
                        max_value=500.0,
                        value=50.0,
                        step=5.0,
                    )
                with b_c2:
                    cabin_input = st.selectbox(
                        "Classe Cabine",
                        options=["ECONOMY", "PREMIUM_ECONOMY", "BUSINESS"],
                        index=0,
                    )

                st.markdown("**📅 Plage de départ autorisée (UTC)**")
                d_c1, d_c2, d_c3, d_c4 = st.columns(4)
                default_start_date = datetime.now(UTC).date()
                default_end_date = default_start_date + timedelta(days=7)
                with d_c1:
                    dep_start_date: date = st.date_input(
                        "Date min", value=default_start_date
                    )
                with d_c2:
                    dep_start_time: dt_time = st.time_input(
                        "Heure min", value=dt_time(0, 0)
                    )
                with d_c3:
                    dep_end_date: date = st.date_input("Date max", value=default_end_date)
                with d_c4:
                    dep_end_time: dt_time = st.time_input(
                        "Heure max", value=dt_time(23, 59)
                    )

                st.markdown("**🔐 Identité & Jeton de Paiement Pré-Autorisé (Chiffrement Simulé)**")
                s_c1, s_c2 = st.columns(2)
                with s_c1:
                    passport_input = st.text_input(
                        "Numéro de Passeport",
                        value="MA88990011",
                        type="password",
                        help="Transmis uniquement au GDS lors de l'émission du PNR.",
                    )
                with s_c2:
                    payment_token_input = st.text_input(
                        "Token de Paiement (Stripe/PSP)",
                        value="tok_visa_autonomous_9981",
                        type="password",
                        help="Empreinte bancaire tokenisée pour débit instantané.",
                    )

                auto_purchase_toggle = st.toggle(
                    "⚡ **Activer l'achat autonome immédiat**",
                    value=True,
                )

                submit_rule = st.form_submit_button(
                    "🛡️ Enregistrer et Armer l'Agent",
                    type="primary",
                    use_container_width=True,
                )

                if submit_rule:
                    earliest_dt = datetime.combine(
                        dep_start_date, dep_start_time, tzinfo=UTC
                    )
                    latest_dt = datetime.combine(dep_end_date, dep_end_time, tzinfo=UTC)
                    masked_token = (
                        f"sha256:{hashlib.sha256(payment_token_input.encode()).hexdigest()[:12]}"
                        if payment_token_input
                        else "tok_none"
                    )

                    rule_payload = {
                        "user_id": user_id_input.strip(),
                        "origin": origin_input,
                        "destination": None if wildcard_dest else destination_input,
                        "max_price": round(float(max_price_input), 2),
                        "currency": "EUR",
                        "preferred_cabin": cabin_input,
                        "earliest_departure": earliest_dt.isoformat(),
                        "latest_departure": latest_dt.isoformat(),
                        "auto_purchase_enabled": bool(auto_purchase_toggle),
                        "passenger_info": {
                            "first_name": first_name_input.strip() or "Voyageur",
                            "last_name": last_name_input.strip() or "Autonome",
                            "email": email_input.strip() or "traveler@example.com",
                            "passport": passport_input.strip() or "PASS998877",
                        },
                    }
                    ok_create, created_rule = api_request(
                        "POST", "/api/v1/rules", json_payload=rule_payload
                    )
                    if ok_create:
                        st.toast(
                            f"Agent armé pour {origin_input} ➔ {destination_input or 'PARTOUT'} "
                            f"(Max {max_price_input:.0f} € | Token {masked_token})",
                            icon="🤖",
                        )
                        st.success(
                            f"✅ Règle enregistrée (`{created_rule.get('id')}`). "
                            f"Statut : **{'ARMÉ & EN ATTENTE' if auto_purchase_toggle else 'INACTIF'}**"
                        )
                        time.sleep(0.4)
                        st.rerun()
                    else:
                        st.error(created_rule)

    with rules_col:
        st.markdown("#### 📋 Règles d'Agents Actives en Base")
        if not rules_list:
            st.info("Aucune règle configurée pour le moment.")
        else:
            for rule in rules_list:
                rule_id = rule["id"]
                is_armed = bool(rule.get("auto_purchase_enabled"))
                dest_display = rule.get("destination") or "🌍 N'IMPORTE OÙ (Wildcard)"
                pax = rule.get("passenger_info") or {}
                pax_name = f"{pax.get('first_name', '')} {pax.get('last_name', '')}".strip()

                with st.container(border=True):
                    rc1, rc2, rc3 = st.columns([2.2, 1.2, 1.1])
                    with rc1:
                        st.markdown(
                            f"**👤 `{rule.get('user_id')}`** ({pax_name or 'Passager'})  \n"
                            f"🛫 **{rule.get('origin')} ➔ {dest_display}** • "
                            f"Cabine : `{rule.get('preferred_cabin')}`"
                        )
                        if rule.get("earliest_departure") or rule.get("latest_departure"):
                            st.caption(
                                f"Fenêtre : {format_datetime_fr(rule.get('earliest_departure'))} → "
                                f"{format_datetime_fr(rule.get('latest_departure'))}"
                            )
                    with rc2:
                        st.markdown(
                            f"Plafond : **{float(rule.get('max_price', 0)):.2f} {rule.get('currency', 'EUR')}**  \n"
                            + (
                                "🟢 `ARMÉ & EN ATTENTE`"
                                if is_armed
                                else "⚪ `INACTIF`"
                            )
                        )
                    with rc3:
                        toggle_label = "Désactiver" if is_armed else "Armer"
                        if st.button(
                            toggle_label,
                            key=f"toggle_rule_{rule_id}",
                            use_container_width=True,
                        ):
                            ok_upd, _ = api_request(
                                "PATCH",
                                f"/api/v1/rules/{rule_id}",
                                json_payload={"auto_purchase_enabled": not is_armed},
                            )
                            if ok_upd:
                                st.rerun()
                        if st.button(
                            "🗑️ Supprimer",
                            key=f"del_rule_{rule_id}",
                            use_container_width=True,
                        ):
                            ok_del, err_del = api_request(
                                "DELETE", f"/api/v1/rules/{rule_id}"
                            )
                            if ok_del:
                                st.toast("Règle supprimée.", icon="🗑️")
                                st.rerun()
                            else:
                                st.error(err_del)


# ----------------------------------------------------------------------------
# ONGLET 3 : SIMULATEUR PARTENAIRE (DÉMO POUR LE JURY)
# ----------------------------------------------------------------------------
with tab_simulator:
    st.subheader("🧪 Simulateur d'Injection Partenaire / Charter (Démo Live Jury)")
    st.markdown(
        "Cet écran simule un **voyagiste ou affréteur charter** qui déstocke en urgence des sièges invendus "
        "quelques heures avant le décollage. Dès l'injection (`POST /api/v1/deals`), l'offre est publiée dans "
        "**Redis Streams**, détectée par le **Booking Engine**, verrouillée via `SET ... NX EX` et achetée en **< 500 ms**."
    )

    sim_col1, sim_col2 = st.columns([1.45, 1.55], gap="large")

    with sim_col1:
        with st.container(border=True):
            st.markdown("#### 🚀 Publier une Offre Flash sur le Marché")
            with st.form("partner_simulator_form", clear_on_submit=False):
                p_c1, p_c2 = st.columns(2)
                with p_c1:
                    sim_flight_no = st.text_input("Numéro de vol", value="AT850")
                    sim_origin = st.selectbox(
                        "Origine (IATA)",
                        options=["CMN", "RBA", "ORY", "CDG", "RAK"],
                        index=0,
                        format_func=format_city_label,
                    )
                    sim_price = st.number_input(
                        "Prix cassé (€)",
                        min_value=5.0,
                        max_value=1000.0,
                        value=35.0,
                        step=5.0,
                    )
                with p_c2:
                    sim_carrier = st.text_input("Code Compagnie (IATA)", value="AT")
                    sim_dest = st.selectbox(
                        "Destination (IATA)",
                        options=["ORY", "CDG", "LIS", "BCN", "JFK", "MAD"],
                        index=0,
                        format_func=format_city_label,
                    )
                    sim_seats = st.number_input(
                        "Nombre de sièges bradés",
                        min_value=1,
                        max_value=50,
                        value=2,
                        step=1,
                    )

                dep_c1, dep_c2 = st.columns(2)
                with dep_c1:
                    hours_until_dep = st.slider(
                        "Départ dans (heures)",
                        min_value=1,
                        max_value=48,
                        value=4,
                    )
                with dep_c2:
                    sim_ref_price = st.number_input(
                        "Prix public de référence (€)",
                        min_value=50.0,
                        max_value=2000.0,
                        value=195.0,
                        step=10.0,
                    )

                sim_saga_fail = st.checkbox(
                    "💥 Simuler une panne GDS lors de l'émission (Démonstration Compensation Saga)",
                    value=False,
                    help="Si coché, l'API GDS renverra une erreur 502 après réservation du siège : le moteur exécutera alors la transaction de compensation (remise en stock du siège + statut COMPENSATED_FAILED).",
                )

                inject_clicked = st.form_submit_button(
                    "🔥 Injecter le vol sur le marché",
                    type="primary",
                    use_container_width=True,
                )

                if inject_clicked:
                    now_utc = datetime.now(UTC)
                    takeoff_utc = now_utc + timedelta(hours=int(hours_until_dep))
                    landing_utc = takeoff_utc + timedelta(hours=3)
                    valid_until_utc = takeoff_utc - timedelta(minutes=30)
                    deal_ref = f"DEMO-{sim_flight_no.strip().upper()}-{uuid.uuid4().hex[:6].upper()}"

                    partner_payload = {
                        "partner_deal_ref": deal_ref,
                        "carrier_iata": sim_carrier.strip().upper()[:8] or "AT",
                        "flight_code": sim_flight_no.strip().upper(),
                        "departure_airport": sim_origin.strip().upper(),
                        "arrival_airport": sim_dest.strip().upper(),
                        "takeoff_utc": takeoff_utc.isoformat(),
                        "landing_utc": landing_utc.isoformat(),
                        "unit_price": round(float(sim_price), 2),
                        "currency": "EUR",
                        "cabin": "ECONOMY",
                        "remaining_seats": int(sim_seats),
                        "offer_valid_until_utc": valid_until_utc.isoformat(),
                        "partner_notes": {
                            "reference_price": float(sim_ref_price),
                            "injected_by": "STREAMLIT_JURY_SIMULATOR",
                            "force_gds_failure": bool(sim_saga_fail),
                            "force_gds_success": not bool(sim_saga_fail),
                        },
                    }

                    t0 = time.perf_counter()
                    ok_inj, inj_res = api_request(
                        "POST",
                        "/api/v1/deals",
                        json_payload=partner_payload,
                        headers={"X-Partner-API-Key": PARTNER_API_KEY},
                    )
                    # Laisser 350 ms au worker Redis Streams pour consommer et émettre le PNR
                    time.sleep(0.35)
                    elapsed_ms = int((time.perf_counter() - t0) * 1000)

                    if ok_inj:
                        canonical_deal_id = inj_res.get("deal_id", f"PARTNER-{deal_ref}")
                        st.session_state["last_injected_deal"] = {
                            "deal_id": canonical_deal_id,
                            "flight_no": sim_flight_no.strip().upper(),
                            "route": f"{sim_origin} ➔ {sim_dest}",
                            "price": sim_price,
                            "seats": sim_seats,
                            "elapsed_ms": elapsed_ms,
                            "stream_id": inj_res.get("stream_message_id"),
                            "saga_fail_mode": sim_saga_fail,
                        }
                        st.toast(
                            "Deal injecté -> Agent alerté -> Achat exécuté en < 500 ms !",
                            icon="🚀",
                        )
                        st.rerun()
                    else:
                        st.error(inj_res)

    with sim_col2:
        st.markdown("#### ⚡ Chronologie d'Exécution Temps Réel")
        last_inj = st.session_state.get("last_injected_deal")
        if not last_inj:
            with st.container(border=True):
                st.info(
                    "👈 Cliquez sur **« 🔥 Injecter le vol sur le marché »** pour déclencher "
                    "la chaîne événementielle complète devant le jury."
                )
                st.markdown(
                    """
                    **Pipeline déclenché au clic :**
                    1. **Normalisation Pydantic (`DealPayload`)** & Upsert PostgreSQL (`flight_deals`).
                    2. **Publication Redis Streams** (`XADD stream:flight_deals`).
                    3. **Consommation `XREADGROUP`** par le `BookingEngineStreamWorker`.
                    4. **Acquisition du Verrou Distribué Redis** (`SET lock:deal:{id} <token> NX EX 15`).
                    5. **Émission PNR auprès du GDS** (ou **Compensation Saga** en cas d'erreur).
                    """
                )
        else:
            st.success("⚡ **Deal injecté -> Agent alerté -> Achat exécuté en < 500 ms !**")
            target_deal_id = last_inj["deal_id"]
            ok_d, updated_deal = api_request("GET", f"/api/v1/deals/{target_deal_id}")
            ok_tx, deal_txs = api_request(
                "GET",
                "/api/v1/rules/bookings/transactions",
                params={"deal_id": target_deal_id},
            )

            with st.container(border=True):
                st.markdown(f"##### 📡 Résultat pour `{target_deal_id}`")
                m1, m2, m3 = st.columns(3)
                m1.metric("Vol & Route", f"{last_inj['flight_no']} ({last_inj['route']})")
                m2.metric("Prix Injecté", f"{float(last_inj['price']):.2f} €")
                m3.metric(
                    "Sièges restants après passage Agent",
                    updated_deal.get("seats_available", "-") if ok_d else "-",
                    delta=f"Initial: {last_inj['seats']}",
                )
                st.caption(f"Redis Stream Message ID : `{last_inj.get('stream_id')}`")

                if ok_tx and isinstance(deal_txs, list) and len(deal_txs) > 0:
                    for tx in deal_txs:
                        if tx.get("status") == "CONFIRMED":
                            st.success(
                                f"🎫 **Billet émis automatiquement** pour `{tx.get('user_id')}` ! "
                                f"Code PNR : **`PNR-{tx.get('pnr_reference')}`** • "
                                f"Montant débité : **{float(tx.get('amount_paid', 0)):.2f} €**"
                            )
                        elif tx.get("status") == "COMPENSATED_FAILED":
                            st.warning(
                                f"🛡️ **Compensation Saga Exécutée** pour `{tx.get('user_id')}` : "
                                f"L'API GDS a échoué (`{tx.get('failure_reason')}`). "
                                "Le siège a été immédiatement remis en stock et le verrou Redis libéré !"
                            )
                else:
                    st.warning(
                        "Aucune règle d'agent active ne correspondait à cet itinéraire ou ce prix. "
                        "Vérifiez dans l'onglet 2 qu'une règle couvre ce trajet et ce budget."
                    )


# ----------------------------------------------------------------------------
# ONGLET 4 : MES RÉSERVATIONS & BILLETS ÉMIS (BOARDING PASS)
# ----------------------------------------------------------------------------
with tab_bookings:
    st.subheader("🎫 Mes Réservations & Cartes d'Embarquement Émises")
    st.write(
        "Historique complet des achats autonomes réalisés par vos agents logiciels, "
        "avec références PNR officielles et journal des transactions Saga."
    )

    confirmed_txs = [b for b in bookings_list if b.get("status") == "CONFIRMED"]
    compensated_txs = [b for b in bookings_list if b.get("status") == "COMPENSATED_FAILED"]

    if not confirmed_txs:
        st.info(
            "Aucun billet confirmé pour le moment. Armez un agent dans l'**Onglet 2** "
            "puis injectez un vol dans l'**Onglet 3 (Simulateur Partenaire)** !"
        )
    else:
        st.markdown(f"#### ✅ Billets Confirmés & Émis ({len(confirmed_txs)})")
        for tx in confirmed_txs:
            deal_id = tx.get("deal_id", "")
            deal_info = deals_by_id.get(deal_id, {})
            rule_info = rules_by_id.get(str(tx.get("rule_id")), {})
            pax_info = rule_info.get("passenger_info") or {}
            passenger_full_name = (
                f"{pax_info.get('first_name', 'Passager')} {pax_info.get('last_name', '')}".strip()
                or tx.get("user_id", "Voyageur")
            )

            raw_pnr = tx.get("pnr_reference") or "A8F29C"
            formatted_pnr = raw_pnr if raw_pnr.startswith("PNR-") else f"PNR-{raw_pnr}"
            flight_no = deal_info.get("flight_number", "VOL-FLASH")
            origin_code = deal_info.get("origin", "---")
            dest_code = deal_info.get("destination", "---")
            dep_time_str = format_datetime_fr(deal_info.get("departure_at"))
            exec_time_str = format_datetime_fr(tx.get("created_at"))
            amount_paid = float(tx.get("amount_paid", 0.0))
            currency = tx.get("currency", "EUR")

            st.markdown(
                f"""
                <div class="boarding-pass">
                    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;">
                        <div>
                            <span style="font-size:0.8rem;text-transform:uppercase;color:#64748b;font-weight:700;">
                                Carte d'Embarquement Électronique • Achat Autonome
                            </span>
                            <div style="font-size:1.55rem;font-weight:900;color:#0f172a;margin-top:2px;">
                                ✈️ {flight_no} &nbsp;|&nbsp; {format_city_label(origin_code)} ➔ {format_city_label(dest_code)}
                            </div>
                        </div>
                        <div style="text-align:right;">
                            <span class="boarding-pass-pnr">{formatted_pnr}</span>
                            <div style="margin-top:6px;">
                                <span class="boarding-pass-status">✓ CONFIRMÉ & ÉMIS</span>
                            </div>
                        </div>
                    </div>
                    <hr style="border:none;border-top:1px dashed #cbd5e1;margin:14px 0;" />
                    <div style="display:grid;grid-template-columns:repeat(4, 1fr);gap:12px;font-size:0.95rem;">
                        <div>
                            <div style="color:#64748b;font-size:0.78rem;text-transform:uppercase;">Passager / Agent</div>
                            <div style="font-weight:700;">👤 {passenger_full_name} (<code>{tx.get('user_id')}</code>)</div>
                        </div>
                        <div>
                            <div style="color:#64748b;font-size:0.78rem;text-transform:uppercase;">Départ du Vol</div>
                            <div style="font-weight:700;">🛫 {dep_time_str}</div>
                        </div>
                        <div>
                            <div style="color:#64748b;font-size:0.78rem;text-transform:uppercase;">Montant Débité</div>
                            <div style="font-weight:800;color:#15803d;font-size:1.1rem;">💶 {amount_paid:.2f} {currency}</div>
                        </div>
                        <div>
                            <div style="color:#64748b;font-size:0.78rem;text-transform:uppercase;">Timestamp d'Exécution</div>
                            <div style="font-weight:700;">⚡ {exec_time_str}</div>
                        </div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    if compensated_txs:
        st.divider()
        with st.expander(
            f"🛡️ Journal des Transactions Compensées — Saga Rollback ({len(compensated_txs)})",
            expanded=True,
        ):
            for ctx in compensated_txs:
                c_deal = deals_by_id.get(ctx.get("deal_id", ""), {})
                st.markdown(
                    f"""
                    <div class="boarding-pass" style="border-left-color:#dc2626;">
                        <div style="display:flex;justify-content:space-between;align-items:center;">
                            <div>
                                <strong>Vol {c_deal.get('flight_number', ctx.get('deal_id'))} ({c_deal.get('origin', '?')} ➔ {c_deal.get('destination', '?')})</strong>
                                — Voyageur : <code>{ctx.get('user_id')}</code>
                                <div style="color:#991b1b;font-size:0.88rem;margin-top:4px;">
                                    Motif GDS : <code>{ctx.get('failure_reason')}</code>
                                </div>
                            </div>
                            <div>
                                <span class="boarding-pass-compensated">↺ COMPENSATED_FAILED (SIÈGE RESTAURÉ)</span>
                            </div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
