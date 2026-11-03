"""Collects new digital movie releases (iTunes chart) + IMDb details into data/movies.json.

Runs on GitHub Actions every few hours. Personal, non-commercial use.
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

FEEDS = {
    "us": "https://itunes.apple.com/us/rss/topmovies/limit=100/json",
    "sa": "https://itunes.apple.com/sa/rss/topmovies/limit=100/json",
}


def get(url, data=None, headers=None, tries=3):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa
            if i == tries - 1:
                print("FAIL", url[:120], e)
                return None
            time.sleep(2 + i * 3)


def lab(e, key, default=""):
    v = e.get(key)
    if isinstance(v, dict):
        return v.get("label", default)
    return default


def parse_date(s):
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%B %d, %Y", "%d %B %Y"):
        try:
            d = datetime.strptime(s, fmt)
            return d.strftime("%Y-%m-%d")
        except Exception:
            pass
    return ""


def itunes(feed):
    j = get(FEEDS[feed])
    out = []
    if not j:
        return out
    for rank, e in enumerate(j["feed"].get("entry", []), 1):
        try:
            iid = e["id"]["attributes"]["im:id"]
            title = lab(e, "im:name")
            imgs = e.get("im:image", [])
            poster = imgs[-1]["label"] if imgs else ""
            poster = re.sub(r"/\d+x\d+bb\.(png|jpg)$", "/600x900bb.jpg", poster)
            links = e.get("link", [])
            if isinstance(links, dict):
                links = [links]
            page = trailer = ""
            dur = 0
            for l in links:
                at = l.get("attributes", {})
                if at.get("rel") == "alternate":
                    page = at.get("href", "")
                if at.get("rel") == "enclosure":
                    trailer = at.get("href", "")
                    try:
                        dur = int(float(l.get("im:duration", {}).get("label", "0")))
                    except Exception:
                        pass
            rel = e.get("im:releaseDate", {}).get("label", "")
            out.append({
                "itunesId": iid,
                "title": re.sub(r"\s*\(\d{4}\)\s*$", "", title),
                "rawTitle": title,
                "genre": e.get("category", {}).get("attributes", {}).get("label", ""),
                "released": parse_date(rel) if rel else "",
                "summary": lab(e, "summary"),
                "director": lab(e, "im:artist"),
                "price": lab(e, "im:price"),
                "rentalPrice": lab(e, "im:rentalPrice"),
                "poster": poster,
                "trailer": trailer,
                "itunes": page.split("?")[0],
                "rank": rank,
                "rights": lab(e, "rights"),
            })
        except Exception as ex:  # noqa
            print("skip entry", ex)
    return out


# ---------------- IMDb ----------------

GQL = "https://api.graphql.imdb.com/"
GQL_HEADERS = {
    "Content-Type": "application/json",
    "x-imdb-client-name": "imdb-web-next-localized",
    "x-imdb-user-language": "en-US",
    "x-imdb-user-country": "US",
    "Origin": "https://www.imdb.com",
    "Referer": "https://www.imdb.com/",
}


def imdb_id(title, year):
    q = title.lower().strip()
    if not q:
        return None
    first = q[0] if q[0].isalnum() else "x"
    j = get("https://v2.sg.media-imdb.com/suggestion/%s/%s.json" % (
        urllib.parse.quote(first), urllib.parse.quote(q)))
    if not j:
        return None
    best, best_score = None, -99
    for e in j.get("d", []):
        iid = e.get("id", "")
        if not iid.startswith("tt"):
            continue
        s = 0
        if e.get("qid") in ("movie", "tvMovie"):
            s += 3
        y = e.get("y") or 0
        if year and y:
            s += {0: 5, 1: 3}.get(abs(y - year), -5)
        if e.get("l", "").lower() == title.lower():
            s += 4
        if s > best_score:
            best, best_score = iid, s
    return best if best_score >= 3 else None


BASIC = """id titleText { text } releaseYear { year }
  ratingsSummary { aggregateRating voteCount }
  runtime { seconds }
  plot { plotText { plainText } }
  genres { genres { text } }
  certificate { rating }"""

EXTRA = """id
  directors: credits(first: 3, filter: { categories: ["director"] }) { edges { node { name { nameText { text } } } } }
  cast: credits(first: 6, filter: { categories: ["actor", "actress"] }) { edges { node { name { nameText { text } } } } }
  metacritic { metascore { score } }"""


def gql(ids, fields):
    out = {}
    for i in range(0, len(ids), 20):
        chunk = ids[i:i + 20]
        q = "{ " + " ".join('a%d: title(id: "%s") { %s }' % (k, t, fields) for k, t in enumerate(chunk)) + " }"
        j = get(GQL, data=json.dumps({"query": q}).encode(), headers=GQL_HEADERS)
        if not j:
            continue
        if j.get("errors"):
            print("GQL errors:", json.dumps(j["errors"])[:500])
        for v in (j.get("data") or {}).values():
            if v and v.get("id"):
                out[v["id"]] = v
    return out


def names(block):
    try:
        return [e["node"]["name"]["nameText"]["text"] for e in block["edges"]]
    except Exception:
        return []


# ---------------- TMDB (release dates + Arabic overview) ----------------

TMDB_KEY = os.environ.get("TMDB_KEY", "").strip()
TMDB = "https://api.themoviedb.org/3"


def tmdb_get(path, **params):
    params["api_key"] = TMDB_KEY
    return get(TMDB + path + "?" + urllib.parse.urlencode(params), tries=2)


def formats_from(notes):
    text = " ".join(notes).lower()
    out = []
    if "4k" in text or "uhd" in text or "ultra hd" in text:
        out.append("4K UHD")
    if "blu" in text:
        out.append("Blu-ray")
    if "dvd" in text:
        out.append("DVD")
    return out


def release_info(rd):
    """From TMDB release_dates: pick US (fallback GB/CA) dates per type."""
    by_country = {r["iso_3166_1"]: r["release_dates"] for r in (rd or {}).get("results", [])}
    info = {}
    for country in ("US", "GB", "CA", "AU"):
        dates = by_country.get(country)
        if not dates:
            continue
        def first(types):
            ds = sorted(d["release_date"][:10] for d in dates if d.get("type") in types and d.get("release_date"))
            return ds[0] if ds else None
        if "theatrical" not in info and first((2, 3)):
            info["theatrical"] = first((2, 3))
        if "digital" not in info and first((4,)):
            info["digital"] = first((4,))
            info["digitalCountry"] = country
        if "physical" not in info and first((5,)):
            phys = [d for d in dates if d.get("type") == 5 and d.get("release_date")]
            info["physical"] = sorted(d["release_date"][:10] for d in phys)[0]
            info["physicalFormats"] = formats_from([d.get("note") or "" for d in phys])
            info["physicalCountry"] = country
    return info


def tmdb_enrich(m, h):
    if not TMDB_KEY or not m.get("imdb"):
        return
    tid = h.get("tmdb")
    if not tid:
        j = tmdb_get("/find/" + m["imdb"], external_source="imdb_id")
        res = (j or {}).get("movie_results") or []
        if not res:
            return
        tid = res[0]["id"]
        h["tmdb"] = tid
    j = tmdb_get("/movie/%s" % tid, language="ar-SA", append_to_response="release_dates")
    if not j:
        return
    m["tmdb"] = tid
    m["titleAr"] = j.get("title") if j.get("title") and j.get("title") != j.get("original_title") else None
    m["overviewAr"] = (j.get("overview") or "").strip() or None
    if j.get("backdrop_path"):
        m["backdrop"] = "https://image.tmdb.org/t/p/w1280" + j["backdrop_path"]
    m.update(release_info(j.get("release_dates")))


def main():
    os.makedirs(DATA, exist_ok=True)
    hist_path = os.path.join(DATA, "history.json")
    try:
        history = json.load(open(hist_path, encoding="utf-8"))
    except Exception:
        history = {}

    us = itunes("us")
    sa = itunes("sa")
    if not us:
        print("No iTunes data; keeping previous file.")
        return
    sa_ids = {m["itunesId"]: m["rank"] for m in sa}

    movies = us + [m for m in sa if m["itunesId"] not in {x["itunesId"] for x in us}]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    us_ids = {x["itunesId"] for x in us}
    for m in movies:
        m["inSaudiStore"] = m["itunesId"] in sa_ids
        m["saRank"] = sa_ids.get(m["itunesId"])
        if m["itunesId"] not in us_ids:
            m["rank"] = 100 + m["rank"]
        h = history.setdefault(m["itunesId"], {"firstSeen": today})
        m["firstSeen"] = h["firstSeen"]

    # IMDb ids (cached in history)
    def find(m):
        h = history[m["itunesId"]]
        if h.get("imdb"):
            return h["imdb"]
        year = int(m["released"][:4]) if m["released"] else None
        y2 = re.search(r"\((\d{4})\)", m["rawTitle"])
        if y2:
            year = int(y2.group(1))
        iid = imdb_id(m["title"], year)
        if iid:
            h["imdb"] = iid
        return iid

    with ThreadPoolExecutor(6) as ex:
        ids = list(ex.map(find, movies))

    want = sorted({i for i in ids if i})
    basic = gql(want, BASIC)
    extra = gql(want, EXTRA)

    for m, iid in zip(movies, ids):
        m["imdb"] = iid
        b = basic.get(iid) or {}
        x = extra.get(iid) or {}
        rs = b.get("ratingsSummary") or {}
        m["rating"] = rs.get("aggregateRating")
        m["votes"] = rs.get("voteCount")
        m["runtime"] = ((b.get("runtime") or {}).get("seconds") or 0) // 60 or None
        m["plot"] = (((b.get("plot") or {}).get("plotText") or {}).get("plainText")) or ""
        m["genres"] = [g["text"] for g in ((b.get("genres") or {}).get("genres") or [])]
        m["certificate"] = (b.get("certificate") or {}).get("rating")
        m["year"] = (b.get("releaseYear") or {}).get("year") or (int(m["released"][:4]) if m["released"] else None)
        m["directors"] = names(x.get("directors")) or ([m["director"]] if m["director"] else [])
        m["cast"] = names(x.get("cast"))
        m["metascore"] = (((x.get("metacritic") or {}).get("metascore")) or {}).get("score")

    if TMDB_KEY:
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(lambda m: tmdb_enrich(m, history[m["itunesId"]]), movies))
        print("tmdb:", sum(1 for m in movies if m.get("tmdb")),
              "digital dates:", sum(1 for m in movies if m.get("digital")),
              "physical:", sum(1 for m in movies if m.get("physical")),
              "arabic overview:", sum(1 for m in movies if m.get("overviewAr")))
    else:
        print("TMDB_KEY not set: skipping release dates")

    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(movies),
        "movies": movies,
    }
    json.dump(out, open(os.path.join(DATA, "movies.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(history, open(hist_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    rated = sum(1 for m in movies if m["rating"])
    print("movies:", len(movies), "imdb ids:", len(want), "rated:", rated,
          "with cast:", sum(1 for m in movies if m["cast"]))


if __name__ == "__main__":
    main()
