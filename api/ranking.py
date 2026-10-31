"""Pure ranking helpers (no Django imports, so they are easy to test).

A candidate is "qualified" for a job once the matching agent accepted them.
Ranking is per job: scores are only ever compared between candidates who applied to the same job.
Ties share a rank (competition ranking: 1, 2, 2, 4).
"""

QUALIFIED_STATUSES = ('matched', 'ranked', 'interview_scheduled')


def competition_rank(score, all_scores):
    """1 + the number of scores that are strictly higher than `score`."""
    return 1 + sum(1 for s in all_scores if s > score)


def build_leaderboard(rows):
    """rows: dicts that contain at least 'score' and 'status'.

    Qualified rows come first, best score first, each with a 'rank'.
    Everyone else follows (rejected, pending...) with rank None.
    """
    qualified = [r for r in rows if r['status'] in QUALIFIED_STATUSES]
    others = [r for r in rows if r['status'] not in QUALIFIED_STATUSES]
    scores = [r['score'] for r in qualified]

    def order(r):
        return (-r['score'], str(r.get('name', '')))

    board = [{**r, 'rank': competition_rank(r['score'], scores)} for r in sorted(qualified, key=order)]
    board += [{**r, 'rank': None} for r in sorted(others, key=order)]
    return board