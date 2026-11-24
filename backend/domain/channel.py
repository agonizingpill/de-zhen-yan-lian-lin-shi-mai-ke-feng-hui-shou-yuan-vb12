from fastapi import HTTPException
from sqlalchemy import and_,func,select
from core.audit import record
from core.db import metadata,transaction
from core.queueing import result,submit
from domain.catalog import calibration,latest,one,same_slot
from domain.interventions import findings,tickets
from domain.learning import relative_to
from domain.soundboard import points,windows
from domain.states import Finding,MIN_SHARED_POINTS,Patch,Ticket
from sqlalchemy import Column,Integer,Table

patch_mutex=Table("patch_serial",metadata,Column("code",Integer,primary_key=True))


def lock_patch(conn):
    conn.execute(select(patch_mutex).with_for_update()).first()


def waiting(conn):
    """Independent findings that can still take a spare line, most severe first.

    A finding stays eligible only while its window is the latest live capture of the
    current wiring revision and no isolation ticket refers to it yet.  Ties on the
    diagnostic score go to the earlier soundcheck.
    """
    newest=(select(windows.c.point.label("point"),windows.c.revision.label("revision"),
                   func.max(windows.c.slot).label("slot"))
            .where(windows.c.purpose=="live")
            .group_by(windows.c.point,windows.c.revision).subquery())
    stmt=(select(findings.c.code,findings.c.focus_window,findings.c.score,
                 windows.c.point,windows.c.channel,windows.c.slot,windows.c.frequency,
                 windows.c.peak_share,windows.c.ring_index)
          .select_from(findings
                       .join(windows,findings.c.focus_window==windows.c.code)
                       .join(points,windows.c.point==points.c.code)
                       .join(newest,and_(newest.c.point==windows.c.point,
                                         newest.c.revision==windows.c.revision,
                                         newest.c.slot==windows.c.slot)))
          .where(findings.c.category==Finding.LOCAL,points.c.patch==Patch.LIVE,
                 points.c.revision==windows.c.revision,
                 ~findings.c.code.in_(select(tickets.c.finding)))
          .order_by(findings.c.score.asc(),windows.c.slot.asc(),findings.c.code.asc()))
    clips={}
    queue=[]
    for row in conn.execute(stmt).mappings().all():
        row=dict(row)
        if row["slot"] not in clips:
            clips[row["slot"]]=same_slot(conn,row)
        if len(set(relative_to(row,clips[row["slot"]])))>=MIN_SHARED_POINTS:
            continue
        queue.append({"code":row["code"],"focus_window":row["focus_window"],"point":row["point"],
                      "channel":row["channel"],"score":row["score"],"slot":row["slot"]})
    return queue


def waiting_list():
    with transaction() as conn:
        return waiting(conn)


def isolate(form,user):
    with transaction() as conn:
        lock_patch(conn)
        note=one(conn,findings,form.finding)
        old=one(conn,windows,note["focus_window"])
        point=one(conn,points,old["point"],lock=True)
        if note["category"]!=Finding.LOCAL:
            raise HTTPException(409,"同频协作或正常结果必须保留，不能据此局部隔离")
        queue=waiting(conn)
        order=[item["code"] for item in queue]
        if form.finding not in order:
            raise HTTPException(409,"该诊断已被更新的声检或隔离单取代，请刷新待改线队列")
        if order[0]!=form.finding:
            raise HTTPException(409,"仍有更严重的独立异常排在队首，改线须依序占用备用路")
        occupied=conn.execute(select(points.c.code).where(points.c.channel==form.destination)).first()
        if form.destination==point["channel"] or occupied:
            raise HTTPException(409,"复线必须使用独立空闲的备用线路")
        # Rewire first and only then issue the ticket; any failure rolls both back so the
        # original wiring and this finding's queue position are kept.
        conn.execute(points.update().where(points.c.code==point["code"]).values(
            patch=Patch.ISOLATED,channel=form.destination,revision=point["revision"]+1))
        conn.execute(tickets.insert().values(code=form.code,point=point["code"],finding=form.finding,
                     old_channel=point["channel"],new_channel=form.destination,state=Ticket.ISOLATED,isolated_by=user["sub"]))
        record(conn,user,f"isolate:{form.code}:{form.destination}")
        return one(conn,tickets,form.code)


def ensure_reline(conn,ticket,focus):
    point=one(conn,points,ticket["point"])
    if ticket["state"]!=Ticket.ISOLATED or point["patch"]!=Patch.ISOLATED:
        raise HTTPException(409,"线路已复位或不在隔离阶段")
    old=one(conn,windows,one(conn,findings,ticket["finding"])["focus_window"])
    if (focus["point"]!=point["code"] or focus["channel"]!=ticket["new_channel"]
        or focus["revision"]!=point["revision"] or focus["purpose"]!="reline" or focus["slot"]<=old["slot"]):
        raise HTTPException(409,"必须使用同一隔离麦位、备用线路及后续时刻的重新声检窗口")
    if latest(conn,point,"reline")["code"]!=focus["code"]:
        raise HTTPException(409,"存在更新的复线窗口，请使用最新窗口")


def probe(code,form,user):
    with transaction() as conn:
        ticket=one(conn,tickets,code)
        focus=one(conn,windows,form.window)
        ensure_reline(conn,ticket,focus)
        return submit(user,{"kind":"reline","ticket":code,"focus":focus,
                            "baseline":calibration(conn),"simultaneous":[]})


def restore(code,form,user):
    data=result(user,form.token)
    payload,rating=data["input"],data["result"]
    if payload["kind"]!="reline" or payload["ticket"]!=code:
        raise HTTPException(409,"计算凭证不属于这张复线单")
    focus=payload["focus"]
    with transaction() as conn:
        lock_patch(conn)
        ticket=one(conn,tickets,code,lock=True)
        point=one(conn,points,ticket["point"],lock=True)
        ensure_reline(conn,ticket,focus)
        if rating["category"]!=Finding.NORMAL or focus["ring_index"]>.36:
            raise HTTPException(409,"复线后仍有异常或明显回响，保留隔离")
        conn.execute(points.update().where(points.c.code==point["code"]).values(patch=Patch.LIVE))
        conn.execute(tickets.update().where(tickets.c.code==code).values(state=Ticket.RESTORED,
                     reline_window=focus["code"],test_score=rating["score"],restored_by=user["sub"]))
        record(conn,user,f"reline:{code}:{focus['code']}")
        return one(conn,tickets,code)
