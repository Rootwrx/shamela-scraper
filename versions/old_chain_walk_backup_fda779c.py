def _chain_walk(
    session: requests.Session,
    book_id: int,
    start_id: int,
    workers: int,
    delay: float,
    progress: bool,
    label: str = "[ids]",
) -> tuple[list[int], dict[int, str]]:
    """
    Walk the linked list of pages forward from start_id (inclusive) by
    following the "load next" button's data-next-id, in speculative
    concurrent batches of `workers`. Stops when the chain genuinely ends
    (no next id) or a fetch fails. Returns (ids_in_order, html_cache) where
    html_cache lets the caller skip re-fetching anything found here.
    """
    ids_in_order: list[int] = []
    html_cache: dict[int, str] = {}

    current_id: int | None = start_id
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while current_id is not None:
            speculative = [current_id + i for i in range(workers)]
            stagger = delay / max(workers, 1)
            futs = {
                pool.submit(_fetch_one, (session, book_id, pid, i * stagger)): pid
                for i, pid in enumerate(speculative)
            }
            results: dict[int, tuple[str | None, str | None]] = {}
            for f in as_completed(futs):
                pid = futs[f]
                _, html, err = f.result()
                results[pid] = (html, err)

            advanced = False
            for pid in speculative:
                html, err = results.get(pid, (None, "not fetched"))
                if err or html is None:
                    break
                next_id = get_next_page_id(html)
                ids_in_order.append(pid)
                html_cache[pid] = html
                current_id = next_id
                advanced = True
                if next_id is None:
                    current_id = None
                    break
                if next_id != pid + 1:
                    break

            if not advanced:
                html, err = results.get(current_id, (None, "no result"))
                if err or html is None:
                    if progress:
                        print(f"\n  [!] chain-walk error at {current_id}: {err}")
                    break
                ids_in_order.append(current_id)
                html_cache[current_id] = html
                current_id = get_next_page_id(html)

            if progress:
                print(f"\r  {label} discovered {len(ids_in_order)} page IDs …",
                      end="", flush=True)

    if progress and ids_in_order:
        print(f"\r  {label} {len(ids_in_order)} page IDs discovered          ")

    return ids_in_order, html_cache


def _chain_walk_until(
    session: requests.Session,
    book_id: int,
    start_id: int,
    stop_id: int,
    workers: int,
    delay: float,
    progress: bool,
    label: str = "[ids:gap]",
) -> tuple[list[int], dict[int, str]]:
    """
    Like _chain_walk, but bounded: walks forward from start_id (which the
    caller already knows is a valid page — e.g. a TOC anchor) and stops as
    soon as the chain reaches or passes stop_id (the next TOC anchor).

    Used to resolve one irregular gap in the TOC (a section the TOC didn't
    label page-by-page) without walking the rest of the book — cost scales
    with the size of *this* gap, not the whole book.

    Returns (ids_in_order, html_cache); start_id itself is NOT included in
    ids_in_order (the caller already has it from the TOC).
    """
    ids_in_order: list[int] = []
    html_cache: dict[int, str] = {}

    # Safety cap: a gap of size N shouldn't ever need to fetch more than a
    # small multiple of N pages to resolve, even with some non-sequential
    # jumps. Guards against an unexpected infinite/very-long walk.
    max_fetches = max(50, (stop_id - start_id) * 3)
    fetched = 0

    current_id: int | None = start_id
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while current_id is not None and current_id < stop_id and fetched < max_fetches:
            speculative = [pid for pid in (current_id + i for i in range(workers)) if pid < stop_id]
            if not speculative:
                break
            stagger = delay / max(workers, 1)
            futs = {
                pool.submit(_fetch_one, (session, book_id, pid, i * stagger)): pid
                for i, pid in enumerate(speculative)
            }
            results: dict[int, tuple[str | None, str | None]] = {}
            for f in as_completed(futs):
                pid = futs[f]
                _, html, err = f.result()
                results[pid] = (html, err)
            fetched += len(speculative)

            advanced = False
            for pid in speculative:
                html, err = results.get(pid, (None, "not fetched"))
                if err or html is None:
                    break
                next_id = get_next_page_id(html)
                if pid != start_id:
                    ids_in_order.append(pid)
                html_cache[pid] = html
                current_id = next_id
                advanced = True
                if next_id is None or next_id >= stop_id:
                    current_id = None
                    break
                if next_id != pid + 1:
                    break

            if not advanced:
                html, err = results.get(current_id, (None, "no result"))
                if err or html is None:
                    if progress:
                        print(f"\n  [!] {label} error at {current_id}: {err}")
                    break
                if current_id != start_id:
                    ids_in_order.append(current_id)
                html_cache[current_id] = html
                current_id = get_next_page_id(html)
                fetched += 1

    return ids_in_order, html_cache


def _discover_page_ids(
