#!/usr/bin/env python3
"""
每日休闲游戏精选报告生成器
数据源：App Store RSS (6国) + Google Play + 微信小游戏 + 抖音小游戏
"""
import json, re, sys, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict
import requests

# ── Config ──────────────────────────────────────────────
REPO_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_DIR / "data"
REPORTS_DIR = REPO_DIR / "reports"
INDEX_FILE = REPO_DIR / "index.html"
FEATURED_FILE = DATA_DIR / "featured.json"
COOLDOWN_DAYS = 30

T1_COUNTRIES = {
    "us": "🇺🇸 美国", "gb": "🇬🇧 英国", "jp": "🇯🇵 日本",
    "kr": "🇰🇷 韩国", "de": "🇩🇪 德国", "fr": "🇫🇷 法国"
}

# ── Utility ─────────────────────────────────────────────
def load_featured():
    """加载已推荐游戏记录"""
    if FEATURED_FILE.exists():
        return json.loads(FEATURED_FILE.read_text())
    return {"featured": {}, "cooldown_days": COOLDOWN_DAYS}

def save_featured(data):
    FEATURED_FILE.parent.mkdir(parents=True, exist_ok=True)
    FEATURED_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def is_on_cooldown(bundle_id, featured_data, today_str):
    """检查游戏是否在冷却期内"""
    game = featured_data["featured"].get(bundle_id)
    if not game:
        return False
    last_date = max(game.get("dates", [""]))
    if not last_date:
        return False
    days_ago = (datetime.strptime(today_str, "%Y-%m-%d") -
                datetime.strptime(last_date, "%Y-%m-%d")).days
    return days_ago < featured_data.get("cooldown_days", COOLDOWN_DAYS)

def mark_featured(bundle_id, name, featured_data, today_str):
    """标记游戏为已推荐"""
    if bundle_id not in featured_data["featured"]:
        featured_data["featured"][bundle_id] = {
            "name": name, "dates": [], "first_seen": today_str
        }
    featured_data["featured"][bundle_id]["dates"].append(today_str)

# ── Data Sources ────────────────────────────────────────

def fetch_appstore_rss():
    """从 App Store RSS 获取 6 个 T1 国家的 Top 25 免费游戏"""
    print("[App Store RSS] Fetching top charts...")
    all_games = {}  # bundleId -> game info
    
    for country, label in T1_COUNTRIES.items():
        url = f"https://itunes.apple.com/{country}/rss/topfreeapplications/limit=25/genre=6014/json"
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            entries = data.get("feed", {}).get("entry", [])
            
            for rank, entry in enumerate(entries, 1):
                name = entry.get("im:name", {}).get("label", "")
                artist = entry.get("im:artist", {}).get("label", "")
                bid = entry.get("id", {}).get("attributes", {}).get("im:bundleId", "")
                link = entry.get("id", {}).get("label", "")
                icon = next((img.get("label","") for img in entry.get("im:image",[]) 
                           if "170x" in img.get("attributes",{}).get("height","")), "")
                cat = entry.get("category", {}).get("attributes", {}).get("label", "")
                release = entry.get("im:releaseDate", {}).get("label", "")
                
                if not bid:
                    continue
                    
                if bid not in all_games:
                    all_games[bid] = {
                        "name": name, "artist": artist, "bundleId": bid,
                        "link": link, "icon": icon, "category": cat,
                        "releaseDate": release,
                        "countries": [], "best_rank": 99, "rank_sum": 0
                    }
                
                g = all_games[bid]
                g["countries"].append(country)
                g["best_rank"] = min(g["best_rank"], rank)
                g["rank_sum"] += rank
            
            print(f"  {country}: {len(entries)} games")
        except Exception as e:
            print(f"  {country}: FAILED - {e}")
    
    print(f"  Total unique games: {len(all_games)}")
    return list(all_games.values())


def fetch_itunes_details(games):
    """通过 iTunes Search API 获取评分、评价数等详细信息"""
    print("[iTunes Search] Fetching details...")
    
    for game in games:
        bid = game["bundleId"]
        try:
            url = f"https://itunes.apple.com/lookup?bundleId={bid}&country=US"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            
            if data.get("resultCount", 0) > 0:
                r = data["results"][0]
                game["rating"] = float(r.get("averageUserRating", 0))
                game["ratingCount"] = int(r.get("userRatingCount", 0))
                game["version"] = r.get("version", "")
                game["artistId"] = int(r.get("artistId", 0))
                game["genres"] = r.get("genres", [])
                game["langs"] = r.get("languageCodesISO2A", [])
                
                # 计算上线天数
                rd = r.get("releaseDate", "")
                if rd:
                    release_dt = datetime.fromisoformat(rd.replace("Z", "+00:00"))
                    days_ago = (datetime.now(timezone.utc) - release_dt).days
                    game["daysSinceRelease"] = max(days_ago, 1)
                    game["dailyReviews"] = game["ratingCount"] // game["daysSinceRelease"]
        except Exception as e:
            print(f"  {bid}: detail fetch failed - {e}")
        
        # Rate limit
        import time; time.sleep(0.1)
    
    return games


def fetch_developer_portfolio(games):
    """通过 artistId 查询开发商的全部游戏"""
    print("[Developer Portfolio] Fetching...")
    portfolio_cache = {}
    
    for game in games:
        aid = game.get("artistId", 0)
        if not aid:
            game["devGameCount"] = 0
            game["devTopGames"] = []
            continue
            
        if aid in portfolio_cache:
            game["devGameCount"] = portfolio_cache[aid]["count"]
            game["devTopGames"] = portfolio_cache[aid]["top"]
            continue
        
        try:
            url = f"https://itunes.apple.com/lookup?id={aid}&entity=software&limit=50&country=US"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            results = [r for r in data.get("results", []) 
                      if r.get("wrapperType") == "software"]
            
            # Filter to games only (primaryGenreId in game range 6000-6023 or 7014+)
            game_results = [r for r in results 
                          if "Games" in str(r.get("primaryGenreName", "")) 
                          or "Game" in str(r.get("genres", []))]
            if not game_results:
                game_results = results  # fallback
            
            top = []
            for r in sorted(game_results, 
                          key=lambda x: x.get("userRatingCount", 0), reverse=True)[:5]:
                top.append({
                    "name": r.get("trackName", ""),
                    "rating": r.get("averageUserRating", 0),
                    "reviews": r.get("userRatingCount", 0)
                })
            
            portfolio_cache[aid] = {
                "count": len(game_results),
                "top": top
            }
            game["devGameCount"] = len(game_results)
            game["devTopGames"] = top
            
        except Exception as e:
            game["devGameCount"] = 0
            game["devTopGames"] = []
        
        import time; time.sleep(0.1)
    
    return games


def fetch_googleplay_top():
    """从 Google Play 获取 Top 免费游戏"""
    print("[Google Play] Fetching top charts...")
    games = []
    
    try:
        from google_play_scraper import app as gp_app
        from google_play_scraper import search as gp_search
        
        # Search for top casual games
        for query in ["casual game", "puzzle game", "arcade game"]:
            try:
                results = gp_search(query, lang="en", country="us", n_hits=10)
                for r in results:
                    games.append({
                        "name": r.get("title", ""),
                        "artist": r.get("developer", ""),
                        "bundleId": f"gp:{r.get('appId','')}",
                        "link": f"https://play.google.com/store/apps/details?id={r.get('appId','')}",
                        "icon": r.get("icon", ""),
                        "category": r.get("genre", ""),
                        "rating": r.get("score", 0),
                        "ratingCount": r.get("ratings", 0),
                        "countries": ["gp"],
                        "best_rank": 99,
                        "rank_sum": 0,
                        "source": "google_play"
                    })
                import time; time.sleep(1)
            except Exception as e:
                print(f"  Query '{query}': {e}")
        
        print(f"  Found: {len(games)} games")
    except ImportError:
        print("  google-play-scraper not available, skipping")
    except Exception as e:
        print(f"  Google Play failed: {e}")
    
    return games


def fetch_wechat_minigames():
    """获取微信小游戏榜单（尝试多个数据源）"""
    print("[WeChat Mini Games] Fetching rankings...")
    games = []
    
    # Source 1: 阿拉丁指数
    try:
        resp = requests.get(
            "https://www.aldzs.com/rank/wechat/list",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15
        )
        if resp.status_code == 200:
            # Try to extract game names from HTML
            names = re.findall(r'title[\s]*[=>"\'][\s]*["\']?([^"\'<>]{2,40})["\'<>]', resp.text)
            seen = set()
            for n in names:
                clean = re.sub(r'<[^>]+>', '', n).strip()
                if clean and len(clean) >= 2 and clean not in seen:
                    seen.add(clean)
                    games.append({
                        "name": clean,
                        "artist": "微信小游戏",
                        "bundleId": f"wxmg:{clean}",
                        "link": "",
                        "icon": "",
                        "category": "小游戏",
                        "countries": ["cn_wx"],
                        "best_rank": 99,
                        "rank_sum": 0,
                        "source": "wechat_minigame"
                    })
            print(f"  aldzs.com: {len(games)} games found")
    except Exception as e:
        print(f"  aldzs.com: {e}")
    
    # Source 2: 微信官方小游戏平台
    if len(games) < 5:
        try:
            resp = requests.get(
                "https://game.weixin.qq.com/cgi-bin/minigame/static/category/list",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    items = data.get("data", {}).get("gameList", [])
                    for item in items[:20]:
                        name = item.get("title", item.get("name", ""))
                        if name:
                            games.append({
                                "name": name,
                                "artist": item.get("developer", "微信小游戏"),
                                "bundleId": f"wxmg:{name}",
                                "link": "",
                                "icon": item.get("icon", ""),
                                "category": "小游戏",
                                "countries": ["cn_wx"],
                                "best_rank": 99,
                                "rank_sum": 0,
                                "source": "wechat_minigame"
                            })
                except json.JSONDecodeError:
                    pass
            print(f"  game.weixin.qq.com: {len(games)} additional")
        except Exception as e:
            print(f"  game.weixin.qq.com: {e}")
    
    return games[:20]  # 最多取 20 个


def fetch_douyin_minigames():
    """获取抖音小游戏榜单"""
    print("[Douyin Mini Games] Fetching rankings...")
    games = []
    
    # 尝试多个第三方数据源
    sources = [
        ("https://www.aldzs.com/rank/douyin/list", "aldzs_dy"),
        ("https://minigame.bytedance.com", "bytedance_official"),
    ]
    
    for url, src_name in sources:
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if resp.status_code == 200:
                names = re.findall(r'title[\s]*[=>"\'][\s]*["\']?([^"\'<>]{2,40})["\'<>]', resp.text)
                seen = {g["name"] for g in games}
                for n in names:
                    clean = re.sub(r'<[^>]+>', '', n).strip()
                    if clean and len(clean) >= 2 and clean not in seen:
                        seen.add(clean)
                        games.append({
                            "name": clean,
                            "artist": "抖音小游戏",
                            "bundleId": f"dymg:{clean}",
                            "link": "",
                            "icon": "",
                            "category": "小游戏",
                            "countries": ["cn_dy"],
                            "best_rank": 99,
                            "rank_sum": 0,
                            "source": "douyin_minigame"
                        })
            print(f"  {src_name}: +{len(games)} games so far")
        except Exception as e:
            print(f"  {src_name}: {e}")
    
    return games[:20]


# ── Scoring ────────────────────────────────────────────

def score_game(game, today_str):
    """综合评分"""
    score = 0
    
    # 1. 跨榜加分：在多个国家上榜
    country_count = len(set(game.get("countries", [])))
    if country_count >= 4:
        score += 5  # 全球化爆款
    elif country_count >= 2:
        score += 3
    elif country_count >= 1:
        score += 1
    
    # 2. 上榜排名加分
    best_rank = game.get("best_rank", 99)
    if best_rank <= 3:
        score += 4
    elif best_rank <= 10:
        score += 2
    elif best_rank <= 25:
        score += 1
    
    # 3. 上线时间加分
    days = game.get("daysSinceRelease", 365)
    if days <= 7:
        score += 5
        game["age_tag"] = "🆕 NEW"
    elif days <= 30:
        score += 4
        game["age_tag"] = "🆕 NEW"
    elif days <= 90:
        score += 2
    else:
        game["age_tag"] = ""
    
    # 4. 日均评价加分
    dr = game.get("dailyReviews", 0)
    if dr > 100:
        score += 5
        game["hot_tag"] = "🔥 HOT"
    elif dr > 50:
        score += 4
        game["hot_tag"] = "🔥 HOT"
    elif dr > 20:
        score += 2
    elif dr > 5:
        score += 1
    else:
        game["hot_tag"] = ""
    
    # 5. 开发商矩阵加分
    dev_count = game.get("devGameCount", 0)
    if dev_count >= 10:
        score += 2
    elif dev_count >= 3:
        score += 1
    
    # 6. 评分加分
    rating = game.get("rating", 0)
    if rating >= 4.5:
        score += 2
    elif rating >= 4.0:
        score += 1
    
    game["score"] = score
    return score


# ── HTML Generation ─────────────────────────────────────

STARS_MAP = {
    5.0: "★★★★★", 4.5: "★★★★½", 4.0: "★★★★☆", 3.5: "★★★½☆",
    3.0: "★★★☆☆", 2.5: "★★½☆☆"
}

def rating_to_stars(rating):
    """评分转星星"""
    r = round(rating * 2) / 2
    return STARS_MAP.get(r, "★★★★★" if r >= 4.8 else "★★★★☆")

def tag_class_for_game(game):
    """根据类型返回 CSS class"""
    cat = game.get("category", "").lower()
    name = game.get("name", "").lower()
    genres = [g.lower() for g in game.get("genres", [])]
    
    if "puzzle" in cat or "puzzle" in str(genres) or "sort" in name:
        return "tag-puzzle"
    elif "casino" in cat or "card" in cat:
        return "tag-casual"
    elif "simulation" in cat or "tycoon" in name:
        return "tag-casual"
    elif "action" in cat or "arcade" in cat:
        return "tag-hyper"
    else:
        return "tag-casual"

def type_label_for_game(game):
    cat = game.get("category", "").lower()
    if "puzzle" in cat: return "解谜"
    if "casino" in cat or "card" in cat: return "棋牌"
    if "simulation" in cat: return "模拟"
    if "action" in cat or "arcade" in cat: return "超休闲"
    if "strategy" in cat: return "策略"
    return "休闲"

def generate_game_card(game, idx):
    """生成单个游戏卡片 HTML"""
    name = game.get("name", "Unknown")
    artist = game.get("artist", "")
    rating = game.get("rating", 0)
    reviews = game.get("ratingCount", 0)
    daily = game.get("dailyReviews", 0)
    stars = rating_to_stars(rating)
    version = game.get("version", "")
    icon = game.get("icon", "")
    link = game.get("link", "")
    days = game.get("daysSinceRelease", 0)
    langs = len(game.get("langs", []))
    genres = game.get("genres", [])
    genre_str = genres[0] if genres else game.get("category", "休闲")
    dev_count = game.get("devGameCount", 0)
    
    tag_class = tag_class_for_game(game)
    type_label = type_label_for_game(game)
    
    age_tag = game.get("age_tag", "")
    hot_tag = game.get("hot_tag", "")
    new_tags = ""
    if age_tag:
        new_tags += f'<span class="tag tag-new">{age_tag}</span>'
    if hot_tag:
        new_tags += f'<span class="tag tag-hot">{hot_tag}</span>'
    
    # Country flags
    countries = game.get("countries", [])
    country_flags = " ".join([{"us":"🇺🇸","gb":"🇬🇧","jp":"🇯🇵","kr":"🇰🇷","de":"🇩🇪","fr":"🇫🇷","cn_wx":"💬","cn_dy":"🎵","gp":"📱"}.get(c,"") for c in countries[:5]])
    
    # Core gameplay placeholder - this is where AI analysis would go
    gameplay = f"在 {country_flags} 多个市场上榜{'，排名最高第' + str(game.get('best_rank','?')) + '位' if game.get('best_rank') else ''}。属于 {genre_str} 类型，由 {artist} 开发。日均获得约 {daily} 条用户评价，活跃用户基数可观。"
    
    highlight = f"该游戏在 {len(set(countries))} 个国家/地区的 App Store 进入 Top 25，{'跨区域吸引力强，玩法具有全球化潜力' if len(set(countries)) >= 3 else '在特定市场表现突出'}。开发商{'拥有 ' + str(dev_count) + ' 款游戏的产品矩阵，发行经验丰富' if dev_count >= 3 else '为新兴开发者，产品迭代速度快'}。"
    
    market = f"评分 {rating}（{reviews:,} 评价），日均 +{daily} 评，版本 {version}，支持 {langs} 种语言。{'在 App Store 多国榜单同时上榜，买量效率高' if len(set(countries)) >= 3 else '区域性增长强劲'}。"
    
    insights = [
        f"玩法核心：{genre_str}品类，{artist} 出品，{'多市场验证的成熟玩法' if dev_count >= 3 else '创意驱动型产品'}",
        f"商业化参考：日均 {daily} 评{'，用户获取成本可控' if daily > 20 else ''}，{'适合混合变现模式' if langs >= 10 else '建议关注本地化策略'}",
        f"出海启示：{'已在 ' + str(len(set(countries))) + ' 个 T1 市场验证，可直接对标' if len(set(countries)) >= 2 else '需观察跨市场复制能力'}"
    ]
    
    card = f"""
<div class="game-card">
<div class="game-header">
<img class="game-icon" src="{icon}" alt="{name}" onerror="this.style.display='none'" loading="lazy">
<div class="game-meta">
<h2>🎮 {name}</h2><div class="dev">{artist} {country_flags}</div>
<div class="rating-row"><span class="stars">{stars}</span><span class="rating-num">{rating}</span><span class="rating-count">（{reviews:,} 评 · 日均 +{daily}）</span></div>
<div class="tags"><span class="tag {tag_class}">{type_label}</span>{new_tags}</div>
</div></div>
<div class="stats-bar">
<div class="stat stat-highlight"><div class="stat-val">{days}天前</div><div class="stat-label">上线</div></div>
<div class="stat"><div class="stat-val">{version}</div><div class="stat-label">版本</div></div>
<div class="stat"><div class="stat-val">{genre_str}</div><div class="stat-label">类型</div></div>
<div class="stat"><div class="stat-val">{langs}语言</div><div class="stat-label">本地化</div></div>
</div>
<div class="dev-info">🏢 <strong>{artist}</strong> · 开发商共 <strong>{dev_count}</strong> 款游戏</div>
<div class="section-label">🎯 核心玩法</div><p class="gameplay-desc">{gameplay}</p>
<div class="highlight-box"><h4>💡 创新亮点</h4><p class="highlight-desc">{highlight}</p></div>
<div class="section-label">📊 市场信号</div><p class="gameplay-desc">{market}</p>
<div class="section-label">🧠 策划启示</div>
<ul class="insights-list">
<li>{insights[0]}</li><li>{insights[1]}</li><li>{insights[2]}</li>
</ul>
<a class="appstore-link" href="{link}" target="_blank" rel="noopener">📱 {'Google Play' if 'play.google.com' in link else 'App Store'} 查看</a>
</div>"""
    
    return card


CSS = r"""
:root{--bg:#0a0a14;--card:#151528;--accent:#7c5cfc;--accent2:#ff6b9d;--gold:#f9ca24;--green:#2ecc71;--blue:#4da6ff;--orange:#ff9f43;--text:#e8e8f0;--text2:#9090b0;--border:#252540;--radius:16px}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans SC",sans-serif;background:var(--bg);color:var(--text);line-height:1.7;min-height:100vh}
.report-header{background:linear-gradient(160deg,#1a1040 0%,#0d1b3e 40%,#0a0a14 100%);border-bottom:1px solid var(--border);padding:36px 24px 28px;text-align:center;position:relative;overflow:hidden}
.report-header::before,.report-header::after{content:'';position:absolute;border-radius:50%}
.report-header::before{top:-50px;right:-50px;width:200px;height:200px;background:radial-gradient(circle,rgba(124,92,252,0.18),transparent 70%)}
.report-header::after{bottom:-30px;left:-30px;width:140px;height:140px;background:radial-gradient(circle,rgba(255,107,157,0.12),transparent 70%)}
.report-header h1{font-size:2rem;font-weight:800;position:relative;z-index:1;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.report-header .date{color:var(--text2);font-size:.9rem;margin-top:6px;position:relative;z-index:1}
.container{max-width:900px;margin:0 auto;padding:28px 20px 60px}
.game-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:28px;margin:28px 0;position:relative;overflow:hidden}
.game-card::after{content:'';position:absolute;top:0;right:0;width:120px;height:120px;background:radial-gradient(circle at top right,rgba(124,92,252,0.06),transparent 70%)}
.game-header{display:flex;gap:20px;align-items:flex-start;margin-bottom:20px;position:relative;z-index:1}
.game-icon{width:80px;height:80px;border-radius:18px;flex-shrink:0;box-shadow:0 8px 24px rgba(0,0,0,0.5);object-fit:cover}
.game-meta{flex:1;min-width:0}.game-meta h2{font-size:1.35rem;font-weight:700;margin-bottom:4px}
.game-meta .dev{color:var(--text2);font-size:.85rem}
.game-meta .tags{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap}
.tag{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.72rem;font-weight:600}
.tag-hyper{background:rgba(255,107,157,0.15);color:var(--accent2)}
.tag-hybrid{background:rgba(77,166,255,0.15);color:var(--blue)}
.tag-casual{background:rgba(46,204,113,0.15);color:var(--green)}
.tag-puzzle{background:rgba(255,159,67,0.15);color:var(--orange)}
.tag-new{background:rgba(249,202,36,0.2);color:var(--gold)}
.tag-hot{background:rgba(255,107,157,0.2);color:var(--accent2)}
.rating-row{display:flex;align-items:center;gap:10px;margin-top:6px}
.stars{color:var(--gold);font-size:1.1rem;letter-spacing:2px}
.rating-num{font-weight:700;font-size:1rem;color:var(--gold)}
.rating-count{color:var(--text2);font-size:.8rem}
.stats-bar{display:flex;gap:20px;flex-wrap:wrap;margin:16px 0;padding:14px 18px;background:rgba(255,255,255,0.03);border-radius:12px;position:relative;z-index:1}
.stat{text-align:center}.stat-val{font-size:1.05rem;font-weight:700;color:var(--accent)}.stat-label{font-size:.72rem;color:var(--text2);margin-top:2px}
.stat-highlight .stat-val{color:var(--gold)}
.section-label{display:inline-flex;align-items:center;gap:6px;font-size:.82rem;font-weight:700;color:var(--accent);margin:18px 0 8px;position:relative;z-index:1}
.section-label::after{content:'';flex:1;height:1px;background:var(--border);min-width:40px;margin-left:4px}
.gameplay-desc,.highlight-desc{font-size:.92rem;line-height:1.75;position:relative;z-index:1}
.highlight-box{background:linear-gradient(135deg,rgba(124,92,252,0.08),rgba(255,107,157,0.05));border:1px solid rgba(124,92,252,0.25);border-radius:12px;padding:16px 20px;margin:14px 0;position:relative;z-index:1}
.highlight-box h4{color:var(--accent);font-size:.85rem;margin-bottom:6px}
.dev-info{font-size:.85rem;color:var(--text2);margin:8px 0;position:relative;z-index:1}
.dev-info strong{color:var(--text)}
.insights-list{list-style:none;padding:0;position:relative;z-index:1}
.insights-list li{padding:8px 0 8px 28px;position:relative;font-size:.9rem;border-bottom:1px solid rgba(255,255,255,0.04)}
.insights-list li:last-child{border-bottom:none}
.insights-list li::before{content:'▸';position:absolute;left:4px;color:var(--accent);font-weight:700}
.trend-card{background:linear-gradient(135deg,rgba(249,202,36,0.06),rgba(124,92,252,0.06));border:1px solid rgba(249,202,36,0.2);border-radius:var(--radius);padding:24px 28px;margin:32px 0 0}
.trend-card h3{color:var(--gold);font-size:1.05rem;margin-bottom:8px}
.trend-card p{font-size:.9rem;color:var(--text2)}
.appstore-link{display:inline-flex;align-items:center;gap:6px;background:rgba(124,92,252,0.12);color:var(--accent);padding:6px 14px;border-radius:20px;text-decoration:none;font-size:.78rem;font-weight:600;margin-top:8px;position:relative;z-index:1}
.appstore-link:hover{background:rgba(124,92,252,0.25)}
.report-footer{text-align:center;padding:30px 20px;color:var(--text2);font-size:.78rem;border-top:1px solid var(--border);margin-top:20px}
.report-footer a{color:var(--accent);text-decoration:none}
@media(max-width:640px){.game-header{flex-direction:column;align-items:center;text-align:center}.game-meta .tags{justify-content:center}.stats-bar{gap:10px;justify-content:center}.game-card{padding:20px 16px}.report-header h1{font-size:1.5rem}}
"""


def generate_report_html(selected_games, today_str, trend_text=""):
    """生成完整 HTML 报告"""
    date_display = datetime.now().strftime("%Y年%m月%d日 %A")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    
    cards = "\n".join(generate_game_card(g, i) for i, g in enumerate(selected_games[:3]))
    
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🎮 每日休闲游戏精选 — {today_str}</title>
<style>{CSS}</style>
</head>
<body>
<header class="report-header">
<h1>🎮 每日休闲游戏精选</h1>
<p class="date">{date_display}</p>
</header>
<div class="container">
{cards}
<div class="trend-card"><h3>💡 今日趋势观察</h3><p>{trend_text}</p></div>
</div>
<footer class="report-footer">
<p>由 GitHub Actions 自动调研生成 · 数据来源 App Store {len(T1_COUNTRIES)}国 / Google Play / 微信小游戏 / 抖音小游戏 · <a href="../">← 返回首页</a></p>
<p style="margin-top:4px">Generated: {timestamp}</p>
</footer>
</body>
</html>"""
    
    return html


def update_index(selected_games, today_str):
    """更新首页，添加新报告链接"""
    badge = datetime.now().strftime("%m月%d日")
    preview = " · ".join([g.get("name", "?") for g in selected_games[:3]])
    
    entry = f'<a class="report-link" href="reports/{today_str}.html"><span class="date-badge">{badge}</span><span class="preview">{preview}</span><span class="arrow">→</span></a>'
    
    if INDEX_FILE.exists():
        html = INDEX_FILE.read_text()
        html = html.replace('<div class="report-list">', f'<div class="report-list">\n        {entry}')
        INDEX_FILE.write_text(html)
        print(f"[Index] Updated with: {preview}")


def generate_trend(selected_games):
    """生成趋势总结"""
    countries_set = set()
    for g in selected_games:
        countries_set.update(g.get("countries", []))
    
    types = [type_label_for_game(g) for g in selected_games]
    avg_daily = sum(g.get("dailyReviews", 0) for g in selected_games) // max(len(selected_games), 1)
    
    parts = []
    if len(countries_set) >= 4:
        parts.append(f"今日精选覆盖 {len(countries_set)} 个国家/地区榜单，呈现全球化竞争格局")
    else:
        parts.append(f"今日精选来自 {', '.join(sorted(countries_set)[:4])} 等区域榜单")
    
    if avg_daily >= 50:
        parts.append(f"三款游戏日均评价均超 50 条，用户获取处于高速增长期")
    
    if "解谜" in types or "puzzle" in str(types):
        parts.append("排序/解谜类持续霸榜，玩法融合（排序+收集/排序+建造）是当前热点")
    if "模拟" in types:
        parts.append("模拟经营品类保持稳定，Meta层深化的趋势明显")
    
    return "。".join(parts) + "。"


# ── Main ────────────────────────────────────────────────

def main():
    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"\n{'='*60}")
    print(f"  🎮 每日休闲游戏精选 — {today_str}")
    print(f"{'='*60}\n")
    
    # Phase 1: Fetch from all sources
    print("── Phase 1: Data Collection ──\n")
    
    appstore_games = fetch_appstore_rss()
    
    # Fetch details for App Store games
    top_30 = sorted(appstore_games, key=lambda g: (g.get("best_rank", 99), g.get("rank_sum", 999)))[:30]
    top_30 = fetch_itunes_details(top_30)
    top_30 = fetch_developer_portfolio(top_30)
    
    # Fetch from other sources
    gp_games = fetch_googleplay_top()
    wx_games = fetch_wechat_minigames()
    dy_games = fetch_douyin_minigames()
    
    # Merge all games, App Store games take priority
    all_candidates = top_30
    seen_names = {g.get("name", "").lower() for g in all_candidates}
    for g in gp_games + wx_games + dy_games:
        if g.get("name", "").lower() not in seen_names:
            all_candidates.append(g)
            seen_names.add(g["name"].lower())
    
    print(f"\n[Total] {len(all_candidates)} candidates across all sources")
    
    # Phase 2: Dedup + Score
    print("\n── Phase 2: Scoring & Selection ──\n")
    
    featured = load_featured()
    
    # Filter out cooldown games
    eligible = []
    for g in all_candidates:
        bid = g.get("bundleId", "")
        if bid and is_on_cooldown(bid, featured, today_str):
            print(f"  SKIP (cooldown): {g.get('name','?')}")
            continue
        score = score_game(g, today_str)
        eligible.append(g)
        if score >= 5:
            print(f"  [{score}] {g['name'][:40]:40} | {g.get('best_rank','?'):2}位 | {g.get('dailyReviews',0):4}/天 | {g.get('devGameCount',0):2}款")
    
    # Sort by score descending, ensure 3 different sub-types
    eligible.sort(key=lambda g: g.get("score", 0), reverse=True)
    
    # Pick top 3 with type diversity
    selected = []
    used_types = set()
    for g in eligible:
        gtype = type_label_for_game(g)
        if gtype not in used_types or len(selected) >= 2:
            selected.append(g)
            used_types.add(gtype)
        if len(selected) >= 3:
            break
    
    print(f"\n[Selected] {len(selected)} games:")
    for i, g in enumerate(selected):
        print(f"  {i+1}. [{g['score']}分] {g['name']} ({type_label_for_game(g)})")
    
    # Mark as featured
    for g in selected:
        mark_featured(g.get("bundleId", ""), g.get("name", ""), featured, today_str)
    save_featured(featured)
    
    # Phase 3: Generate Report
    print("\n── Phase 3: Report Generation ──\n")
    
    trend = generate_trend(selected)
    html = generate_report_html(selected, today_str, trend)
    
    report_path = REPORTS_DIR / f"{today_str}.html"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path.write_text(html)
    print(f"  Report: {report_path} ({len(html):,} bytes)")
    
    update_index(selected, today_str)
    
    # Summary
    print(f"\n{'='*60}")
    print(f"  ✅ DONE — {today_str}")
    print(f"  📄 {report_path}")
    print(f"  🌐 https://lrhehe.github.io/casual-games-daily/reports/{today_str}.html")
    print(f"{'='*60}\n")
    
    # Print summary table
    print("| 游戏 | 类型 | 评分 | 日均评价 | 上线 | 上榜国家 |")
    print("|------|------|------|----------|------|----------|")
    for g in selected:
        countries = " ".join([c.upper() for c in g.get("countries", [])[:4]])
        print(f"| {g['name'][:25]} | {type_label_for_game(g)} | {g.get('rating',0)} | +{g.get('dailyReviews',0)}/天 | {g.get('daysSinceRelease',0)}天前 | {countries} |")


if __name__ == "__main__":
    main()
