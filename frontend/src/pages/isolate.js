import {el} from '../dom.js';
import {mixer} from '../audio-api.js';
import {channels,desk,labels,patchNames} from '../audio-state.js';
import {field,run} from '../audio-form.js';
import {status,titleBlock} from '../widgets.js';
import {badge} from '../components/badge.js';
import {wiring} from '../components/lines.js';

export async function isolate(root,role) {
  const board=el('section',{class:'ledger'});
  const edit=el('section',{class:'action'});
  async function load(message) {
    const [patch,findings,queue]=await Promise.all([mixer.patch(),mixer.findings(),mixer.queue()]);
    const head=queue[0];
    board.replaceChildren(el('h2',{},'接线状态'),...wiring(patch.points),
      el('h2',{},'待改线队列'),
      queue.length?el('ol',{class:'queue'},queue.map((item,index)=>el('li',{'data-head':index===0?'':undefined},
        el('code',{},item.code),' · '+item.point+' · '+item.channel+
        ' · LOF '+item.score.toFixed(4)+' · 刻度 '+item.slot,
        index===0?badge({head:'队首'},'head'):null))):
        status('暂无仍有效的独立异常。新增声检或已开隔离单都会使旧诊断退出队列。'),
      el('h2',{},'同频协作保留记录'),...findings.filter(item=>item.category==='coordinated').map(item=>
        el('p',{class:'note'},item.code+' · '+item.shared_points.join(' / ')+' ',badge(labels,item.category))),
      el('h2',{},'隔离单'),...patch.tickets.map(item=>el('p',{},item.code+' · '+item.point+
        ' · '+item.old_channel+' → '+item.new_channel+' ',badge(patchNames,item.state))));
    edit.replaceChildren(el('h2',{},'仅变更单麦位'),message?status(message,'ok'):null);
    if (role!=='reviewer'||!head) {
      edit.append(status(role==='reviewer'?'队首改线完成后再轮到下一条独立异常。':'复核员可对队列中的本地异常申请独立备用路。'));
      return;
    }
    const busy=new Set(patch.points.map(point=>point.channel));
    const spare=channels.filter(code=>!busy.has(code));
    edit.append(el('p',{class:'note'},'当前队首 '+head.code+' · '+head.point+' · LOF '+head.score.toFixed(4)));
    if (!spare.length) {
      edit.append(status('全部线路均有麦位驻留，须等既有隔离单复位后才能改线。','error'));
      return;
    }
    edit.append(run('isolate',[
      field('code','隔离单号',{placeholder:'ISO-1124-HUB'}),
      field('finding','队首诊断',{value:head.code,readonly:''}),
      el('label',{},el('span',{},'备用线路'),el('select',{name:'destination',required:''},
        spare.map(code=>el('option',{value:code},code+' · 独立空闲')))),
    ],'隔离并切至备用路',async form=>{
      const item=await mixer.isolate(Object.fromEntries(form));
      desk.ticket=item.code;
      await load(item.code+' 已隔离，等待复线试音。');
    }));
  }
  root.replaceChildren(titleBlock('安澜 · 仅单点变更','异常麦位隔离','每张隔离单只改动一个麦位。队列按诊断分数排序，同分先到先排；仅队首可占用空闲备用路。'),
    el('div',{class:'mix'},board,edit));
  await load();
}
