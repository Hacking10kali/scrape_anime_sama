"""
scraper_gha.py
--------------
Scraper anime-sama.to pour GitHub Actions.
Pour chaque anime : récupère le mal_id via Jikan, sinon l'imdb_id via OMDB/IMDB.
"""

import asyncio
import json
import aiohttp
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# ── Utils ───────────────────────────────────────────────────────────────────────

def save_json(data, filename: str):
    output_dir = Path("data")
    output_dir.mkdir(exist_ok=True)
    path = output_dir / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"💾 Sauvegardé : {path}")


# ── ID Lookup ───────────────────────────────────────────────────────────────────

async def get_mal_id(session: aiohttp.ClientSession, titre: str) -> int | None:
    """Cherche le mal_id sur Jikan (MyAnimeList) par titre."""
    try:
        url = "https://api.jikan.moe/v4/anime"
        params = {"q": titre, "limit": 1}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            results = data.get("data", [])
            if results:
                mal_id = results[0].get("mal_id")
                print(f"   ✅ MAL trouvé  : {titre} → mal_id={mal_id}")
                return mal_id
    except Exception as e:
        print(f"   ⚠️  Jikan erreur pour '{titre}' : {e}")
    return None


async def get_imdb_id(session: aiohttp.ClientSession, titre: str) -> str | None:
    """Fallback : cherche l'imdb_id via l'API de recherche IMDB (suggestions)."""
    try:
        # Utilise l'API de suggestions IMDB (pas besoin de clé)
        query = titre.replace(" ", "_")
        url = f"https://v2.sg.media-imdb.com/suggestion/x/{query}.json"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            results = data.get("d", [])
            # Filtre sur les types séries/films (tt = title)
            for r in results:
                imdb_id = r.get("id", "")
                if imdb_id.startswith("tt"):
                    print(f"   ✅ IMDB trouvé : {titre} → imdb_id={imdb_id}")
                    return imdb_id
    except Exception as e:
        print(f"   ⚠️  IMDB erreur pour '{titre}' : {e}")
    return None


async def resolve_ids(session: aiohttp.ClientSession, titre: str) -> dict:
    """
    Tente d'abord MAL, puis fallback IMDB.
    Retourne {"mal_id": int|None, "imdb_id": str|None}
    """
    mal_id = await get_mal_id(session, titre)
    if mal_id:
        return {"mal_id": mal_id, "imdb_id": None}

    print(f"   🔄 MAL non trouvé pour '{titre}', tentative IMDB...")
    imdb_id = await get_imdb_id(session, titre)
    return {"mal_id": None, "imdb_id": imdb_id if imdb_id else None}


# ── Scraping ────────────────────────────────────────────────────────────────────

async def scrape_planning_page(page, session: aiohttp.ClientSession) -> list[dict]:
    print("\n📅 Extraction du planning...")
    planning_data = []

    jours = await page.query_selector_all("div.fadeJours")
    for jour in jours:
        titre_elem = await jour.query_selector("h2.titreJours")
        titre_jour = (await titre_elem.inner_text()).strip() if titre_elem else "Jour Inconnu"

        jour_data = {"jour": titre_jour, "animes": []}

        cartes = await jour.query_selector_all("div.anime-card-premium")
        for carte in cartes:
            titre_elem = await carte.query_selector(".card-title")
            titre      = (await titre_elem.inner_text()).strip() if titre_elem else "Titre Inconnu"

            heure_elem = await carte.query_selector(".info-text.font-bold")
            heure      = (await heure_elem.inner_text()).strip() if heure_elem else "Heure Inconnue"

            saison     = "Saison Inconnue"
            for info in await carte.query_selector_all(".info-text"):
                cls = await info.get_attribute("class")
                if cls and "font-bold" not in cls:
                    saison = (await info.inner_text()).strip()
                    break

            badge_elem = await carte.query_selector(".badge-text")
            badge      = (await badge_elem.inner_text()).strip() if badge_elem else "Inconnu"

            langues = []
            if await carte.query_selector('img[title="VF"]'):     langues.append("VF")
            if await carte.query_selector('img[title="VOSTFR"]'): langues.append("VOSTFR")

            # Résolution des IDs
            ids = await resolve_ids(session, titre)
            # Jikan rate limit : 1 req/seconde recommandé
            await asyncio.sleep(0.5)

            jour_data["animes"].append({
                "titre":        titre,
                "heure_sortie": heure,
                "saison":       saison,
                "format":       badge,
                "langue":       " & ".join(langues) if langues else "Inconnue",
                "mal_id":       ids["mal_id"],
                "imdb_id":      ids["imdb_id"],
            })

        planning_data.append(jour_data)

    total = sum(len(j["animes"]) for j in planning_data)
    print(f"   → {len(planning_data)} jour(s), {total} anime(s) traité(s).")
    return planning_data


async def scrape_recent_animes(page, context, session: aiohttp.ClientSession) -> list[dict]:
    print("\n🆕 Extraction des derniers épisodes...")
    recent_data = []

    container = await page.query_selector("#containerAjoutsAnimes")
    if not container:
        print("   ⚠️  Conteneur #containerAjoutsAnimes introuvable.")
        return recent_data

    cartes = await container.query_selector_all("div.anime-card-premium")
    print(f"   → {len(cartes)} épisodes récents trouvés.")

    for carte in cartes:
        lien_elem = await carte.query_selector("a")
        lien_url  = await lien_elem.get_attribute("href") if lien_elem else None

        titre_elem = await carte.query_selector(".card-title")
        titre      = (await titre_elem.inner_text()).strip() if titre_elem else "Titre Inconnu"

        episode_info = " ".join([
            (await i.inner_text()).strip()
            for i in await carte.query_selector_all(".info-text")
            if (await i.inner_text()).strip()
        ])

        langues = []
        if await carte.query_selector('img[title="VF"]'):     langues.append("VF")
        if await carte.query_selector('img[title="VOSTFR"]'): langues.append("VOSTFR")

        badge_elem = await carte.query_selector(".badge-text")
        badge      = (await badge_elem.inner_text()).strip() if badge_elem else "Inconnu"

        print(f"   → {titre} | {episode_info}")

        if lien_url and not lien_url.startswith("http"):
            lien_url = "https://anime-sama.to" + (lien_url if lien_url.startswith("/") else "/" + lien_url)

        # Lecteurs
        lecteurs = []
        if lien_url:
            ep_page = await context.new_page()
            try:
                await ep_page.goto(lien_url, wait_until="domcontentloaded", timeout=30000)
                try:
                    await ep_page.wait_for_selector("#selectLecteurs", timeout=3000)
                    options = await ep_page.eval_on_selector_all(
                        "#selectLecteurs option",
                        "els => els.map(e => ({value: e.value, text: e.textContent.trim()}))"
                    )
                    for opt in options:
                        if opt["value"]:
                            try:
                                await ep_page.select_option("#selectLecteurs", opt["value"])
                                await ep_page.wait_for_timeout(300)
                            except Exception:
                                pass
                        try:
                            iframe = await ep_page.query_selector("#playerDF")
                            src    = await iframe.get_attribute("src") if iframe else None
                        except Exception:
                            src = None
                        lecteurs.append({"nom": opt["text"], "url": src})
                except PlaywrightTimeoutError:
                    try:
                        iframe = await ep_page.query_selector("#playerDF")
                        src    = await iframe.get_attribute("src") if iframe else None
                        if src:
                            lecteurs.append({"nom": "Défaut", "url": src})
                    except Exception:
                        pass
            except Exception as e:
                print(f"     ⚠️  Erreur navigation {lien_url} : {e}")
            finally:
                await ep_page.close()

        # Résolution des IDs
        ids = await resolve_ids(session, titre)
        await asyncio.sleep(0.5)

        recent_data.append({
            "titre":        titre,
            "episode_info": episode_info,
            "format":       badge,
            "langue":       " & ".join(langues) if langues else "Inconnue",
            "lien":         lien_url,
            "lecteurs":     lecteurs,
            "mal_id":       ids["mal_id"],
            "imdb_id":      ids["imdb_id"],
        })

    return recent_data


# ── Main ────────────────────────────────────────────────────────────────────────

async def main():
    url = "https://anime-sama.to/"

    async with aiohttp.ClientSession() as session:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="fr-FR",
                timezone_id="Europe/Paris",
            )

            page = await context.new_page()
            print(f"🌐 Navigation vers {url}...")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            try:
                await page.wait_for_selector("div.fadeJours", timeout=15000)
            except PlaywrightTimeoutError:
                print("⚠️  Timeout : section planning non trouvée.")

            planning = await scrape_planning_page(page, session)
            recents  = await scrape_recent_animes(page, context, session)

            await browser.close()

    if planning:
        save_json(planning, "planning_anime_sama.json")
        print(f"\n📅 Planning : {sum(len(j['animes']) for j in planning)} animes sur {len(planning)} jours")
    if recents:
        save_json(recents, "ajouts_recents_anime_sama.json")
        print(f"🆕 Récents  : {len(recents)} épisodes")


if __name__ == "__main__":
    asyncio.run(main())
