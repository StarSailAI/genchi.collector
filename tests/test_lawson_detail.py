import copy
import json

import pytest
from bs4 import BeautifulSoup
from genchi_fetchers.fetchers import _pia_window
from genchi_fetchers.lawson import detail_url, merge_details, parse_detail
from genchi_product.domain import Moment, classify
from genchi_product.pipeline import structured
from genchi_product.schedules import role_for, schedule_node


def native_data():
    return {
        "result": 0,
        "lCode": "12345",
        "evName": "学園アイドルマスター LIVE",
        "selectEntryMthd": "02",
        "selectSalesScheduleNo": "1",
        "salesSystemBeanList": [
            {
                "entryMethod": "02",
                "scheduleNo": "1",
                "entryName": "先着",
                "receptionName": "一般発売",
                "reservationStartDateTime": "2026-08-01T10:00:00+09:00",
                "reservationEndDateTime": "2026-09-19T23:59:00+09:00",
            }
        ],
        "pfInfoListBean": {
            "pfInfoDetailListBeanList": [
                {
                    "pfIdCls": "1",
                    "pfDateYYYYMMDD": "20260920",
                    "pfDate": "2026/9/20(日)",
                    "venueName": "Tokyo Hall",
                    "prefName": "東京都",
                    "pfInfoDetailList": [
                        {
                            "pfKey": "key-1",
                            "curtainCharDisp": "開演",
                            "curtainTime": "13:00",
                            "openGateTime": "12:00",
                            "salesEndDateTime": "2026-09-18T22:00:00+09:00",
                            "entryStatusName": "予定枚数終了",
                        },
                        {
                            "pfKey": "key-2",
                            "curtainCharDisp": "開演",
                            "curtainTime": "18:00",
                            "openGateTime": "17:00",
                            "salesEndDateTime": "2026-09-19T22:00:00+09:00",
                        },
                    ],
                }
            ]
        },
    }


def parsed(data=None):
    return parse_detail(
        '<script id="form-data" type="application/json">'
        + json.dumps(data or native_data())
        + "</script>",
        detail_url("12345"),
    )


def test_native_same_day_sessions_and_deadlines_stay_distinct():
    page = parsed()
    a, b = page["events"]
    assert a["startsAt"] == "2026-09-20T13:00:00+09:00"
    assert b["startsAt"] == "2026-09-20T18:00:00+09:00"
    assert a["doorsAt"] == "2026-09-20T12:00:00+09:00"
    wa, wb = a["ticketWindows"][0], b["ticketWindows"][0]
    assert wa["closesAt"] != wb["closesAt"]
    assert wa["roundId"] == wb["roundId"] and wa["id"] != wb["id"]
    assert wa["status"] == "CLOSED"  # sold out is never a canceled performance
    assert wa["nativePerformanceKeys"] == ["key-1"]
    assert len(merge_details([page, page])) == 2
    conflict = copy.deepcopy(page)
    conflict["events"][0]["startLabel"] = "入店開始"
    with pytest.raises(ValueError, match="Conflicting"):
        merge_details([page, conflict])


def test_period_native_sentinel_is_not_midnight_or_fake_session():
    data = native_data()
    row = data["pfInfoListBean"]["pfInfoDetailListBeanList"][0]
    row.update(pfIdCls="2", pfDateYYYYMMDD="99999999", pfDate="2026/8/22(土) ～ 2026/9/29(火)")
    row["pfInfoDetailList"] = [{"pfKey": "pass", "curtainTime": "", "curtainCharDisp": ""}]
    event = parsed(data)["events"][0]
    assert event["startsAt"] == "2026-08-22" and event["endsAt"] == "2026-09-29"
    with pytest.raises(ValueError, match="identity differs"):
        parse_detail(
            '<script id="form-data" type="application/json">' + json.dumps(data) + "</script>",
            detail_url("98765"),
        )


def resource(page):
    return {
        "id": 1,
        "source_id": "lawson",
        "external_id": "lawson:12345",
        "content_hash": "a",
        "title": "学園アイドルマスター LIVE",
        "attributes": {
            "source_type": "lawson_ticket",
            "ticket_page": {"platform": "lawson", **page},
        },
        "tags": ["project:school-idolmaster"],
    }


def test_detail_completeness_and_native_scope_are_required():
    page = parsed()
    for completeness in ["search_summary", "partial_detail"]:
        with pytest.raises(ValueError):
            structured(resource({**page, "scheduleCompleteness": completeness}), [])
    invalid = copy.deepcopy(page)
    invalid["events"][0]["ticketWindows"][0]["nativePerformanceKeys"] = ["key-2"]
    with pytest.raises(ValueError, match="适用关系"):
        structured(resource(invalid), [])
    items = structured(resource(page), [])
    assert len(items) == 2 and items[0].milestones[0].scope_key != items[1].milestones[0].scope_key
    entry = copy.deepcopy(page)
    entry["events"][0]["startLabel"] = "入店開始"
    assert structured(resource(entry), [])[0].publication == "REVIEW"


@pytest.mark.parametrize(
    "title,source_label,role,node_kind,action",
    [
        ("ラブライブ！活動展 ～Aqours～", "開演", "ADMISSION", "DOORS", "指定入场"),
        ("MAPPA EXPO", "入場開始", "ADMISSION", "DOORS", "指定入场"),
        ("ホテル ブルーロック ROOM", "開演", "STAY", "START", "办理入住"),
        ("プロセカ ライブビューイング", "開演", "SCREENING", "START", "放映开始"),
        ("学園アイドルマスター LIVE", "開演", "PERFORMANCE", "START", "开演"),
    ],
)
def test_schedule_semantics(title, source_label, role, node_kind, action):
    assert role_for(title, "OTHER", source_label) == role
    moment = Moment(precision="TIME", starts_at="2026-09-20T13:00:00+09:00")
    kind, label, _ = schedule_node(title, "OTHER", moment, "会場", role=role)
    assert kind == node_kind and action in label and "13:00" in label
    assert classify("ラブライブ！活動展 ～Aqours～") == "EXHIBITION"


def test_pia_cancellation_disclaimer_is_not_cancellation():
    soup = BeautifulSoup(
        '<span class="textLabel">発売中</span><dl class="dataList"><dt>受付期間</dt><dd>2026/9/1(火) 10:00 ～ 2026/9/10(木) 23:59</dd></dl><footer>公演中止の場合の払い戻しについて</footer>',
        "lxml",
    )
    window = _pia_window(
        soup, sale_id="a", sale_url="https://t.pia.jp/a", fallback_label="一般発売"
    )
    assert window and window["status"] != "CANCELED"


def test_pia_uses_specific_sale_round_without_changing_existing_identity():
    soup = BeautifulSoup(
        '<span class="textLabel--title">先行抽選</span><dl class="dataList">'
        '<dt>受付期間</dt><dd>2026/9/1(火) 18:00 ～ 2026/9/17(木) 23:59</dd></dl>',
        "lxml",
    )
    ordinary = _pia_window(soup, sale_id="a", sale_url="https://t.pia.jp/a",
                           fallback_label="先行抽選")
    specific = _pia_window(soup, sale_id="a", sale_url="https://t.pia.jp/a",
                           fallback_label="「SEKAI NO OWARI ARENA TOUR 2027」オフィシャル先行")
    assert ordinary and specific
    assert specific["label"] == "オフィシャル先行"
    assert specific["id"] == ordinary["id"]


def test_venue_aliases_are_scoped_and_virtual_platforms_are_not_halls():
    from genchi_product.venues import bundle_venue, nonphysical_venue, venue_key

    title = "Aqours スクールアイドル活動展 Dive into Sparkle"
    assert venue_key("東京建物ぴあカンファレンス", title, "2026") == venue_key(
        "東京建物ぴあカンファレンス TO YAESU HALL", title, "2026"
    )
    assert venue_key("東京建物ぴあカンファレンス", "Another event", "2026") != venue_key(
        "東京建物ぴあカンファレンス TO YAESU HALL", "Another event", "2026"
    )
    assert venue_key("東京建物ぴあカンファレンス", title, "2027") != venue_key(
        "東京建物ぴあカンファレンス TO YAESU HALL", title, "2027"
    )
    assert nonphysical_venue("ＰＩＡ ＬＩＶＥ ＳＴＲＥＡＭ")
    assert bundle_venue("Shibuya LOVEZ【3公演通しチケット】")
    page = parsed()
    page["events"][0]["venue"]["name"] = "SPWN"
    assert structured(resource(page), [])[0].attendance == "ONLINE"
    page["events"][0]["venue"]["name"] = "Shibuya LOVEZ【3公演通しチケット】"
    assert structured(resource(page), [])[0].publication == "REVIEW"
