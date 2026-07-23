from flask import Flask, jsonify, render_template, request

import ea_client

app = Flask(__name__)


def error_response(exc):
    status = exc.status_code if isinstance(exc.status_code, int) else 502
    return jsonify({"error": str(exc)}), status


@app.route("/")
def index():
    return render_template("index.html", platforms=ea_client.PLATFORMS)


@app.route("/api/platforms")
def platforms():
    return jsonify(ea_client.PLATFORMS)


@app.route("/api/clubs/search")
def search():
    name = request.args.get("name", "").strip()
    platform = request.args.get("platform", "")
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        return jsonify(ea_client.search_club(platform, name))
    except ea_client.EAApiError as exc:
        return error_response(exc)


@app.route("/api/clubs/<club_id>/overview")
def overview(club_id):
    platform = request.args.get("platform", "")
    try:
        info = ea_client.club_info(platform, club_id)
        stats = ea_client.overall_stats(platform, club_id)
    except ea_client.EAApiError as exc:
        return error_response(exc)
    if not info and not stats:
        return jsonify({"error": "club not found"}), 404
    return jsonify({"info": info, "stats": stats})


@app.route("/api/clubs/<club_id>/standings")
def standings(club_id):
    platform = request.args.get("platform", "")
    try:
        division = ea_client.division_stats(platform, club_id)
        stats = ea_client.overall_stats(platform, club_id)
    except ea_client.EAApiError as exc:
        return error_response(exc)
    if not division and not stats:
        return jsonify({"error": "club not found"}), 404
    division = division or {}
    stats = stats or {}
    return jsonify(
        {
            "currentDivision": division.get("currentDivision"),
            "bestDivision": division.get("bestDivision") or stats.get("bestDivision"),
            "points": division.get("points"),
            "bestFinishGroup": stats.get("bestFinishGroup"),
            "skillRating": stats.get("skillRating"),
            "promotions": stats.get("promotions") or division.get("promotions"),
            "relegations": stats.get("relegations") or division.get("relegations"),
            "wstreak": stats.get("wstreak"),
            "unbeatenstreak": stats.get("unbeatenstreak"),
            "leagueAppearances": stats.get("leagueAppearances"),
        }
    )


@app.route("/api/clubs/<club_id>/members")
def members(club_id):
    platform = request.args.get("platform", "")
    try:
        current = ea_client.member_stats(platform, club_id) or {}
        career = ea_client.member_career_stats(platform, club_id) or {}
    except ea_client.EAApiError as exc:
        return error_response(exc)

    career_by_name = {m.get("name"): m for m in career.get("members", [])}
    merged = []
    for m in current.get("members", []):
        row = dict(m)
        c = career_by_name.get(m.get("name"))
        if c:
            row["careerGoals"] = c.get("goals")
            row["careerAssists"] = c.get("assists")
            row["careerGamesPlayed"] = c.get("gamesPlayed")
            row["careerManOfTheMatch"] = c.get("manOfTheMatch")
            row["careerRatingAve"] = c.get("ratingAve")
        merged.append(row)

    return jsonify(
        {"members": merged, "positionCount": current.get("positionCount", {})}
    )


@app.route("/api/clubs/<club_id>/matches")
def matches(club_id):
    platform = request.args.get("platform", "")
    match_type = request.args.get("matchType", "leagueMatch")
    try:
        count = int(request.args.get("count", 10))
    except ValueError:
        return jsonify({"error": "count must be an integer"}), 400
    count = max(1, min(count, 30))  # EA doesn't return much beyond ~30 anyway
    try:
        data = ea_client.matches_stats(platform, club_id, match_type, max_results=count)
    except ea_client.EAApiError as exc:
        return error_response(exc)
    return jsonify(data)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
