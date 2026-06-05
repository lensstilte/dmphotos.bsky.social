        repost_uri = getattr(out, "uri", None)
        if repost_uri:
            repost_records[subject_uri] = repost_uri
    except Exception as e:
        log(f"⚠️ Repost error: {e}")
        return False

    try:
        out_like = client.app.bsky.feed.like.create(
            repo=me,
            record={
                "subject": {"uri": subject_uri, "cid": subject_cid},
                "createdAt": dt_to_iso(utcnow()),
            },
        )
        like_uri = getattr(out_like, "uri", None)
        if like_uri:
            like_records[subject_uri] = like_uri
    except Exception as e:
        log(f"⚠️ Like error: {e}")

    return True


def apply_anti_cluster(cands: List[Dict], lookahead: int = 3) -> List[Dict]:
    if not cands:
        return cands

    result = cands[:]
    for i in range(len(result) - 2):
        a1 = result[i]["author_key"]
        a2 = result[i + 1]["author_key"]
        a3 = result[i + 2]["author_key"]

        if a1 == a2 == a3:
            swap_idx = None
            for j in range(i + 3, min(len(result), i + 3 + lookahead)):
                if result[j]["author_key"] != a1:
                    swap_idx = j
                    break
            if swap_idx is not None:
                result[i + 2], result[swap_idx] = result[swap_idx], result[i + 2]
    return result


def cleanup_old_reposts(
    client: Client,
    me: str,
    repost_records: Dict[str, str],
    like_records: Dict[str, str],
    post_last_reposted_at: Dict[str, str],
    days: int,
    max_per_run: int,
) -> int:
    cutoff = utcnow() - timedelta(days=days)
    cleaned = 0

    for subject_uri, repost_uri in list(repost_records.items()):
        if cleaned >= max_per_run:
            break

        last_dt = parse_iso_dt(post_last_reposted_at.get(subject_uri))
        if not last_dt or last_dt > cutoff:
            continue

        parsed = parse_at_uri_rkey(repost_uri)
        if parsed:
            did, collection, rkey = parsed
            if did == me and collection == "app.bsky.feed.repost":
                try:
                    client.app.bsky.feed.repost.delete({"repo": me, "rkey": rkey})
                except Exception as e:
                    log(f"⚠️ Cleanup unrepost failed: {e}")

        if subject_uri in like_records:
            like_uri = like_records.get(subject_uri)
            like_parsed = parse_at_uri_rkey(like_uri) if like_uri else None
            if like_parsed:
                did, collection, rkey = like_parsed
                if did == me and collection == "app.bsky.feed.like":
                    try:
                        client.app.bsky.feed.like.delete({"repo": me, "rkey": rkey})
                    except Exception as e:
                        log(f"⚠️ Cleanup unlike failed: {e}")
            like_records.pop(subject_uri, None)

        repost_records.pop(subject_uri, None)
        post_last_reposted_at.pop(subject_uri, None)
        cleaned += 1

    return cleaned


def main():
    log("=== DMPHOTOS BOT START ===")

    username = os.getenv(ENV_USERNAME, "").strip()
    password = os.getenv(ENV_PASSWORD, "").strip()
    if not username or not password:
        log(f"❌ Missing env {ENV_USERNAME} / {ENV_PASSWORD}")
        return

    cutoff = utcnow() - timedelta(hours=HOURS_BACK)

    state = load_state(STATE_FILE)
    repost_records: Dict[str, str] = state.get("repost_records", {})
    like_records: Dict[str, str] = state.get("like_records", {})
    post_last_reposted_at: Dict[str, str] = state.get("post_last_reposted_at", {})
    self_random_history: List[str] = state.get("self_random_history", [])

    client = Client()
    client.login(username, password)
    me = client.me.did
    log(f"✅ Logged in as {me}")

    if CLEANUP_ENABLED == 1:
        cleaned = cleanup_old_reposts(
            client,
            me,
            repost_records,
            like_records,
            post_last_reposted_at,
            CLEANUP_DAYS,
            CLEANUP_MAX_PER_RUN,
        )
        if cleaned:
            log(f"🧹 Cleanup done: {cleaned}")

    feed_uris: List[Tuple[str, Dict, str]] = []
    for key, obj in FEEDS.items():
        if int(obj.get("enabled", 0)) != 1:
            continue
        link = (obj.get("link") or "").strip()
        if not link:
            continue
        uri = normalize_feed_uri(client, link)
        if uri:
            feed_uris.append((key, obj, uri))
        else:
            log(f"⚠️ Feed ongeldig (skip): {key} -> {link}")

    list_uris: List[Tuple[str, Dict, str]] = []
    for key, obj in LIJSTEN.items():
        if int(obj.get("enabled", 0)) != 1:
            continue
        link = (obj.get("link") or "").strip()
        if not link:
            continue
        uri = normalize_list_uri(client, link)
        if uri:
            list_uris.append((key, obj, uri))
        else:
            log(f"⚠️ Lijst ongeldig (skip): {key} -> {link}")

    excl_uris: List[Tuple[str, Dict, str]] = []
    for key, obj in EXCLUDE_LISTS.items():
        link = (obj.get("link") or "").strip()
        if not link:
            continue
        uri = normalize_list_uri(client, link)
        if uri:
            excl_uris.append((key, obj, uri))
        else:
            log(f"⚠️ Exclude lijst ongeldig (skip): {key} -> {link}")

    exclude_handles: Set[str] = set()
    exclude_dids: Set[str] = set()
    for key, obj, luri in excl_uris:
        note = obj.get("note", "")
        log(f"🚫 Loading exclude list: {key} ({note})")
        members = fetch_list_members(client, luri, limit=max(1000, LIST_MEMBER_LIMIT))
        log(f"🚫 Exclude members: {len(members)}")
        for h, d in members:
            if h:
                exclude_handles.add(h)
            if d:
                exclude_dids.add(d)

    all_candidates: List[Dict] = []

    log(f"Feeds to process: {len(feed_uris)}")
    for key, obj, furi in feed_uris:
        note = obj.get("note", "")
        log(f"📥 Feed: {key} ({note})")

        items = fetch_feed_items(client, furi, max_items=FEED_MAX_ITEMS)
        cands = build_candidates_from_feed_items(
            items=items,
            cutoff=cutoff,
            exclude_handles=exclude_handles,
            exclude_dids=exclude_dids,
            allow_posts=int(obj.get("allow_posts", 1)),
            allow_replies=int(obj.get("allow_replies", 0)),
            allow_reposts=int(obj.get("allow_reposts", 0)),
            force_refresh=False,
            promo_bucket=None,
        )
        all_candidates.extend(cands)

    log(f"Lists to process: {len(list_uris)}")
    for key, obj, luri in list_uris:
        note = obj.get("note", "")
        members = fetch_list_members(client, luri, limit=max(1000, LIST_MEMBER_LIMIT))
        total_accounts = len(members)
        active_accounts = 0

        tag = ""
        if key == PROMO_RANDOM_LIST_KEY:
            tag = " [PROMO RANDOM]"
        elif key == PROMO_LATEST_LIST_KEY:
            tag = " [PROMO LATEST]"

        log(f"📋 List: {key} ({note}){tag} -> {total_accounts} accounts")

        for h, d in members:
            actor = d or h
            if not actor:
                continue

            if key == PROMO_RANDOM_LIST_KEY:
                author_items = fetch_author_feed(client, actor, PROMO_FETCH_PER_MEMBER)
                cand = pick_random_weighted_candidate(
                    author_items,
                    exclude_handles,
                    exclude_dids,
                    PROMO_RANDOM_POOL,
                )                if cand and not uri_in_cooldown(cand["uri"], post_last_reposted_at, POST_COOLDOWN_HOURS):
                    cand["promo_bucket"] = "promo_random"
                    all_candidates.append(cand)
                    active_accounts += 1

                elif key == PROMO_LATEST_LIST_KEY:
                author_items = fetch_author_feed(client, actor, PROMO_FETCH_PER_MEMBER)
                cand = pick_latest_candidate(
                    author_items,
                    exclude_handles,
                    exclude_dids,
                    "promo_latest",
                )
                if cand and not uri_in_cooldown(cand["uri"], post_last_reposted_at, POST_COOLDOWN_HOURS):
                    all_candidates.append(cand)
                    active_accounts += 1

            else:
                author_items = fetch_author_feed(client, actor, AUTHOR_POSTS_PER_MEMBER)
                cands = build_candidates_from_feed_items(
                    items=author_items,
                    cutoff=cutoff,
                    exclude_handles=exclude_handles,
                    exclude_dids=exclude_dids,
                    allow_posts=int(obj.get("allow_posts", 1)),
                    allow_replies=int(obj.get("allow_replies", 0)),
                    allow_reposts=int(obj.get("allow_reposts", 0)),
                    force_refresh=False,
                    promo_bucket=None,
                )
                if cands:
                    active_accounts += 1
                    all_candidates.extend(cands)

        if total_accounts >= 1400:
            log(f"⚠️ {key} bijna limiet: {total_accounts} accounts")
        elif total_accounts >= 1000:
            log(f"⚠️ {key} groot: {total_accounts} accounts")

        log(f"📊 {key}: total_accounts={total_accounts} | active_accounts={active_accounts}")

    active_hashtags = [h.strip() for h in HASHTAGS if h.strip()]
    log(f"Hashtags to process: {len(active_hashtags)}")
    for query in active_hashtags:
        log(f"🔎 Hashtag search: {query}")
        posts = fetch_hashtag_posts(client, query, HASHTAG_MAX_ITEMS)
        log(f"Hashtag posts fetched for {query}: {len(posts)}")
        all_candidates.extend(
            build_candidates_from_postviews(posts, cutoff, exclude_handles, exclude_dids)
        )

    if SELF_RANDOM.get("enabled", 0) == 1:
        self_cand = build_self_random_candidate(
            client=client,
            me_did=me,
            exclude_handles=exclude_handles,
            exclude_dids=exclude_dids,
            pool_size=int(SELF_RANDOM.get("pool_size", 25)),
            self_random_history=self_random_history,
        )
        if self_cand and not uri_in_cooldown(self_cand["uri"], post_last_reposted_at, POST_COOLDOWN_HOURS):
            all_candidates.append(self_cand)
            log("🎲 Self-random candidate toegevoegd")

    seen: Set[str] = set()
    deduped: List[Dict] = []
    for c in all_candidates:
        uri = c.get("uri")
        if not uri or uri in seen:
            continue
        seen.add(uri)
        deduped.append(c)

    normal_cands = [c for c in deduped if not c.get("promo_bucket")]
    promo_latest_cands = [c for c in deduped if c.get("promo_bucket") == "promo_latest"]
    promo_random_cands = [c for c in deduped if c.get("promo_bucket") == "promo_random"]
    self_random_cands = [c for c in deduped if c.get("promo_bucket") == "self_random"]

    normal_cands.sort(key=lambda x: x["created"])
    promo_latest_cands.sort(key=lambda x: x["created"])
    promo_random_cands.sort(key=lambda x: x["created"])
    self_random_cands.sort(key=lambda x: x["created"])

    normal_cands = apply_anti_cluster(normal_cands)

    log(
        "🧩 Candidates total: "
        f"{len(deduped)} | normal={len(normal_cands)} "
        f"| promo_latest={len(promo_latest_cands)} "
        f"| promo_random={len(promo_random_cands)} "
        f"| self_random={len(self_random_cands)}"
    )

    total_done = 0
    per_user_count: Dict[str, int] = {}

    buckets = {
        "normal": normal_cands,
        "promo_latest": promo_latest_cands,
        "promo_random": promo_random_cands,
        "self_random": self_random_cands,
    }

    ordered_groups = sorted(
        [(k, v) for k, v in PROCESS_ORDER.items() if v > 0],
        key=lambda x: x[1]
    )

    group_posted_count: Dict[str, int] = {k: 0 for k in buckets.keys()}

    for group_name, _ in ordered_groups:
        group = buckets.get(group_name, [])

        for c in group:
            if total_done >= MAX_PER_RUN:
                break

            is_promo = group_name != "normal"
            ak = c["author_key"]

            if not is_promo:
                per_user_count.setdefault(ak, 0)
                if per_user_count[ak] >= MAX_PER_USER:
                    continue

            ok = repost_and_like(
                client=client,
                me=me,
                subject_uri=c["uri"],
                subject_cid=c["cid"],
                repost_records=repost_records,
                like_records=like_records,
                force_refresh=is_promo,
            )

            if ok:
                total_done += 1
                group_posted_count[group_name] += 1
                post_last_reposted_at[c["uri"]] = dt_to_iso(utcnow())

                if not is_promo:
                    per_user_count[ak] += 1

                if group_name == "self_random":
                    self_random_history.append(c["uri"])
                    self_random_history = self_random_history[-10:]

                log(f"Repost [{group_name}]: {c['uri']}")
                time.sleep(SLEEP_SECONDS)

    log(
        "Posted per group: "
        + " | ".join(f"{k}={group_posted_count.get(k, 0)}" for k in buckets.keys())
    )

    state["repost_records"] = repost_records
    state["like_records"] = like_records
    state["post_last_reposted_at"] = post_last_reposted_at
    state["self_random_history"] = self_random_history

    save_state(STATE_FILE, state)

    log(f"🔥 Done — total reposts this run: {total_done}")


if __name__ == "__main__":
    try:
        print("=== ABOUT TO CALL MAIN ===", flush=True)
        main()
    except Exception:
        import traceback
        print("=== FATAL ERROR ===", flush=True)
        traceback.print_exc()
        raise
                
    buckets = {
        "normal": normal_cands,
        "promo_latest": promo_latest_cands,
        "promo_random": promo_random_cands,
        "self_random": self_random_cands,
    }

    ordered_groups = sorted(
        [(k, v) for k, v in PROCESS_ORDER.items() if v > 0],
        key=lambda x: x[1]
    )

    group_posted_count: Dict[str, int] = {k: 0 for k in buckets.keys()}

    for group_name, _ in ordered_groups:
        group = buckets.get(group_name, [])

        for c in group:
            if total_done >= MAX_PER_RUN:
                break

            is_promo = group_name != "normal"
            ak = c["author_key"]

            if not is_promo:
                per_user_count.setdefault(ak, 0)
                if per_user_count[ak] >= MAX_PER_USER:
                    continue

            ok = repost_and_like(
                client=client,
                me=me,
                subject_uri=c["uri"],
                subject_cid=c["cid"],
                repost_records=repost_records,
                like_records=like_records,
                force_refresh=is_promo,
            )

            if ok:
                total_done += 1
                group_posted_count[group_name] += 1
                post_last_reposted_at[c["uri"]] = dt_to_iso(utcnow())

                if not is_promo:
                    per_user_count[ak] += 1

                if group_name == "self_random":
                    self_random_history.append(c["uri"])
                    self_random_history = self_random_history[-10:]

                log(f"Repost [{group_name}]: {c['uri']}")
                time.sleep(SLEEP_SECONDS)

    log(
        "Posted per group: "
        + " | ".join(f"{k}={group_posted_count.get(k, 0)}" for k in buckets.keys())
    )

    state["repost_records"] = repost_records
    state["like_records"] = like_records
    state["post_last_reposted_at"] = post_last_reposted_at
    state["self_random_history"] = self_random_history

    save_state(STATE_FILE, state)

    log(f"🔥 Done — total reposts this run: {total_done}")


if __name__ == "__main__":
    try:
        print("=== ABOUT TO CALL MAIN ===", flush=True)
        main()
    except Exception:
        import traceback
        print("=== FATAL ERROR ===", flush=True)
        traceback.print_exc()
        raise
