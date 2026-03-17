from sqlalchemy import text
from sqlalchemy.orm import Session


def count_perfect_brackets(session: Session) -> int:
    # Perfect = no wrong picks among games that have results
    q = text("""
    with played as (
      select game_id, winner_team_id
      from real_results
    ),
    wrong as (
      select bp.bracket_id
      from bracket_picks bp
      join played p on p.game_id = bp.game_id
      where bp.predicted_winner_team_id <> p.winner_team_id
      group by bp.bracket_id
    )
    select count(*)::int
    from brackets b
    where not exists (select 1 from wrong w where w.bracket_id = b.id);
    """)
    return session.execute(q).scalar_one()


def leaderboard(session: Session, limit: int = 25):
    # Score = number of correct picks so far (only games with results)
    q = text("""
    with played as (
      select game_id, winner_team_id
      from real_results
    ),
    scored as (
      select
        bp.bracket_id,
        sum(case when bp.predicted_winner_team_id = p.winner_team_id then 1 else 0 end)::int as correct,
        count(*)::int as decided
      from bracket_picks bp
      join played p on p.game_id = bp.game_id
      group by bp.bracket_id
    )
    select
      b.id as bracket_id,
      coalesce(s.correct, 0) as correct,
      coalesce(s.decided, 0) as decided
    from brackets b
    left join scored s on s.bracket_id = b.id
    order by correct desc, decided desc, bracket_id asc
    limit :limit;
    """)
    return session.execute(q, {"limit": limit}).mappings().all()


def pick_percentages_by_round(session: Session, round_num: int):
    # % of brackets picking each team to win games in this round
    # denominator = number of brackets (not number of picks), so champion % is straightforward.
    q = text("""
    with n as (select count(*)::float as n from brackets),
    picks as (
      select bp.bracket_id, bp.predicted_winner_team_id as team_id
      from bracket_picks bp
      join tournament_games tg on tg.id = bp.game_id
      where tg.round = :round_num
    ),
    by_team as (
      select team_id, count(*)::float as picks
      from picks
      group by team_id
    )
    select
      t.name,
      t.seed,
      t.region,
      bt.picks,
      (bt.picks / nullif((select n from n), 0)) as pct
    from by_team bt
    join teams t on t.id = bt.team_id
    order by pct desc, t.seed asc, t.name asc;
    """)
    return session.execute(q, {"round_num": round_num}).mappings().all()