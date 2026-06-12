"""Tests for the orthogonal obstacle-avoidance SLD router."""

from esfex.visualization.sld.router import GridRouter, Obstacles


def _bar(x0, x1, y, gid, h=6.0):
    return (x0, x1, y, y + h, gid)


class TestObstacles:
    def test_h_blocked_by_leg_in_span(self):
        ob = Obstacles([], [(100.0, 0.0, 50.0)])
        assert ob.h_blocked(50.0, 150.0, 25.0) is True      # leg crosses lane
        assert ob.h_blocked(50.0, 150.0, 60.0) is False     # lane below the leg
        assert ob.h_blocked(150.0, 250.0, 25.0) is False    # leg outside span

    def test_v_blocked_pierces_bar(self):
        ob = Obstacles([_bar(0.0, 100.0, 200.0, "b")], [])
        # vertical at x=50 from y=150 to y=260 passes through the bar [200,206]
        assert ob.v_blocked(50.0, 150.0, 260.0) is True
        # ignoring that bar → clear
        assert ob.v_blocked(50.0, 150.0, 260.0, ignore_bars={"b"}) is False
        # outside the bar's x-range → clear
        assert ob.v_blocked(150.0, 150.0, 260.0) is False

    def test_v_blocked_endpoint_touch_is_clear(self):
        """A riser that starts on a bar's plane and goes AWAY from it only
        touches the bar edge — it must not count as piercing it."""
        ob = Obstacles([_bar(0.0, 100.0, 0.0, "b")], [])
        # segment from y=0 (bar top plane) up to y=-200 only touches at y=0
        assert ob.v_blocked(50.0, 0.0, -200.0) is False

    def test_v_blocked_by_leg_overlap(self):
        ob = Obstacles([], [(40.0, 100.0, 300.0)])
        assert ob.v_blocked(40.0, 150.0, 250.0) is True     # runs along the leg
        assert ob.v_blocked(40.0, 350.0, 400.0) is False    # below the leg


class TestGridRouter:
    def test_straight_clear_route(self):
        ob = Obstacles([_bar(0.0, 40.0, 0.0, "a"), _bar(200.0, 240.0, 0.0, "b")], [])
        r = GridRouter(ob, lane_ys=[-30.0, -58.0], x_tracks=[-50.0, 120.0, 290.0],
                       lane_step=28.0)
        pts = r.route(20.0, 0.0, "a", 220.0, 0.0, "b")
        assert pts is not None
        assert pts[0] == (20.0, 0.0)
        assert pts[-1] == (220.0, 0.0)
        # every segment is orthogonal
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            assert abs(x0 - x1) < 0.5 or abs(y0 - y1) < 0.5

    def test_routes_around_a_blocking_leg(self):
        """A leg sits between the two bars in the only direct channel; the
        router must detour to a clear lane and never cross the leg."""
        bars = [_bar(0.0, 40.0, 0.0, "a"), _bar(200.0, 240.0, 0.0, "b")]
        leg = (120.0, 0.0, 40.0)                      # leg just below the row
        ob = Obstacles(bars, [leg])
        r = GridRouter(ob, lane_ys=[20.0, -30.0, -58.0],
                       x_tracks=[-50.0, 120.0, 290.0], lane_step=28.0)
        pts = r.route(20.0, 0.0, "a", 220.0, 0.0, "b")
        assert pts is not None
        # No horizontal segment may cross the leg's x at a y within its span.
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if abs(y0 - y1) < 0.5:                    # horizontal
                lo, hi = sorted((x0, x1))
                if lo < 120.0 < hi:
                    assert not (0.0 <= y0 <= 40.0), f"crosses leg at {y0}"
