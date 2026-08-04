"""The cross-unit social feed — status posts, comments, and likes shared by
every member across every unit on the platform. It's cross-unit by nature
(the same wall for a member of any regiment, clan, or squad), so it lives in
the registry alongside alliances rather than in any one unit's database.

v1 is self-moderated: a member can delete their own posts and comments, but
there is no officer/admin moderation of other members' content.
"""
from __future__ import annotations

from tenancy.registry import Post, PostComment, PostLike, registry_session

POST_MAX_LEN = 2000
COMMENT_MAX_LEN = 1000


class FeedError(Exception):
    """Raised for a rejected feed action (empty body, not found, not yours)."""


def create_post(author: dict, body: str, image: str | None = None) -> int:
    body = (body or "").strip()
    if not body and not image:
        raise FeedError("Say something, or attach an image.")
    if len(body) > POST_MAX_LEN:
        raise FeedError(f"Posts are limited to {POST_MAX_LEN} characters.")
    with registry_session() as s:
        post = Post(
            author_discord_id=int(author["id"]), author_name=author["name"],
            author_avatar=author.get("avatar"), body=body, image=image,
        )
        s.add(post)
        s.commit()
        return post.id


def add_comment(author: dict, post_id: int, body: str) -> str:
    body = (body or "").strip()
    if not body:
        raise FeedError("Comment can't be empty.")
    if len(body) > COMMENT_MAX_LEN:
        raise FeedError(f"Comments are limited to {COMMENT_MAX_LEN} characters.")
    with registry_session() as s:
        if s.get(Post, post_id) is None:
            raise FeedError("That post no longer exists.")
        s.add(PostComment(
            post_id=post_id, author_discord_id=int(author["id"]),
            author_name=author["name"], author_avatar=author.get("avatar"), body=body,
        ))
        s.commit()
    return "Comment posted."


def toggle_like(discord_id: int, post_id: int) -> bool:
    """Like or unlike a post. Returns True if the post is now liked, False
    if the existing like was just removed."""
    with registry_session() as s:
        if s.get(Post, post_id) is None:
            raise FeedError("That post no longer exists.")
        existing = (
            s.query(PostLike)
            .filter(PostLike.post_id == post_id, PostLike.discord_id == discord_id)
            .one_or_none()
        )
        if existing is None:
            s.add(PostLike(post_id=post_id, discord_id=discord_id))
            s.commit()
            return True
        s.delete(existing)
        s.commit()
        return False


def delete_post(discord_id: int, post_id: int) -> None:
    with registry_session() as s:
        post = s.get(Post, post_id)
        if post is None:
            raise FeedError("That post no longer exists.")
        if post.author_discord_id != discord_id:
            raise FeedError("You can only delete your own posts.")
        s.query(PostComment).filter(PostComment.post_id == post_id).delete()
        s.query(PostLike).filter(PostLike.post_id == post_id).delete()
        s.delete(post)
        s.commit()


def delete_comment(discord_id: int, comment_id: int) -> None:
    with registry_session() as s:
        comment = s.get(PostComment, comment_id)
        if comment is None:
            raise FeedError("That comment no longer exists.")
        if comment.author_discord_id != discord_id:
            raise FeedError("You can only delete your own comments.")
        s.delete(comment)
        s.commit()


def list_posts(viewer_id: int | None = None, limit: int = 30, before_id: int | None = None) -> list[dict]:
    """The feed, newest first, each post with its comments and like info."""
    with registry_session() as s:
        q = s.query(Post).order_by(Post.id.desc())
        if before_id is not None:
            q = q.filter(Post.id < before_id)
        posts = q.limit(limit).all()
        post_ids = [p.id for p in posts]
        if not post_ids:
            return []

        comments_by_post: dict[int, list] = {}
        for c in (
            s.query(PostComment)
            .filter(PostComment.post_id.in_(post_ids))
            .order_by(PostComment.created_at.asc())
        ):
            comments_by_post.setdefault(c.post_id, []).append({
                "id": c.id, "author_discord_id": c.author_discord_id,
                "author_name": c.author_name, "author_avatar": c.author_avatar,
                "body": c.body, "created_at": c.created_at,
                "can_delete": viewer_id is not None and c.author_discord_id == viewer_id,
            })

        like_counts: dict[int, int] = {}
        liked_by_me: set[int] = set()
        for like in s.query(PostLike).filter(PostLike.post_id.in_(post_ids)):
            like_counts[like.post_id] = like_counts.get(like.post_id, 0) + 1
            if viewer_id is not None and like.discord_id == viewer_id:
                liked_by_me.add(like.post_id)

        return [{
            "id": p.id, "author_discord_id": p.author_discord_id,
            "author_name": p.author_name, "author_avatar": p.author_avatar,
            "body": p.body, "image": p.image, "created_at": p.created_at,
            "comments": comments_by_post.get(p.id, []),
            "like_count": like_counts.get(p.id, 0),
            "liked_by_me": p.id in liked_by_me,
            "can_delete": viewer_id is not None and p.author_discord_id == viewer_id,
        } for p in posts]
