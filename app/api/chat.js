'use strict';
const $=id=>document.getElementById(id);
let session=null,busy=false,pollTimer=null,pollCount=0,user=null,renderedMessages='';
const incoming=new URLSearchParams(location.search);
const returnConversation=incoming.get('conversation'),paymentReturn=incoming.get('payment'),returnOrder=incoming.get('order_id');
// Retire the old private-link token; account credentials live only in HttpOnly cookies.
sessionStorage.removeItem('lifestore-token');
if(location.hash)history.replaceState(null,'',location.pathname+location.search);
async function api(path,body){
 const response=await fetch(path,{credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-LifeStore-Request':'1'},...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)})});
 let data;try{data=await response.json()}catch{throw Error('The service is unavailable. Please try again.')}
 if(response.status===401){showLogin();throw Error(data.detail||'Please sign in again.')}
 if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'Please check your details and try again.');
 return data;
}
function showLogin(){clearTimeout(pollTimer);user=null;$('loading').hidden=true;$('app').hidden=true;$('account').hidden=true;$('login-panel').hidden=false;$('messages').replaceChildren();$('checkout').replaceChildren();$('history').replaceChildren();}
function add(text,role){const div=document.createElement('div');div.className='message '+role;div.textContent=text;$('messages').append(div);$('messages').scrollTop=$('messages').scrollHeight;}
function renderMessages(data){renderedMessages=JSON.stringify(data.messages||[]);$('messages').replaceChildren();if(!data.messages?.length)add('Hi! What are you looking for today? Ask me about a product or choose a question to get started.','assistant');else for(const m of data.messages)add(m.text,m.role);}
function setBusy(value){busy=value;for(const el of document.querySelectorAll('#app button,#logout'))el.disabled=value;}
function setSession(id){session=id;history.replaceState(null,'','/?conversation='+encodeURIComponent(id));}
async function refreshHistory(){const data=await api('/api/conversations');$('history').replaceChildren();if(!data.conversations.length){const p=document.createElement('p');p.className='empty';p.textContent='Start your first conversation.';$('history').append(p)}for(const c of data.conversations){const b=document.createElement('button');b.textContent=c.title;b.title=c.title;b.className=c.id===session?'active':'';b.disabled=busy;b.onclick=()=>openConversation(c.id).catch(showError);$('history').append(b)}return data.conversations;}
function showError(error){$('status').textContent=error.message;}
function renderCheckout(data){
 const container=$('checkout');container.replaceChildren();$('payment-banner').hidden=true;
 if(data.checkout){const review=data.checkout,box=document.createElement('div');box.className='review';const summary=document.createElement('pre');summary.textContent='Review your order (sandbox)\n\n'+review.cart.items.map(i=>i.name+' × '+i.quantity+' — Rs. '+Number(i.subtotal).toLocaleString('en-LK')).join('\n')+'\n\nTotal: Rs. '+Number(review.cart.total).toLocaleString('en-LK')+'\n\n'+Object.values(review.customer).join('\n');box.append(summary);const actions=document.createElement('div');actions.className='actions';for(const [action,label] of [['confirm','Confirm order'],['cancel','Cancel checkout']]){const b=document.createElement('button');b.textContent=label;b.className=action==='confirm'?'primary':'secondary';b.disabled=busy;b.onclick=()=>submitChat('',action);actions.append(b)}box.append(actions);container.append(box)}
 for(const order of data.orders||[]){const box=document.createElement('div');box.className='order';const title=document.createElement('strong');title.textContent='Order '+order.order_id;const state=document.createElement('p');state.textContent=(order.status==='PAID'?'Payment confirmed':order.status==='PAYMENT_FAILED'?'Payment failed — you can try again':'Awaiting payment confirmation')+' · Rs. '+Number(order.total).toLocaleString('en-LK');if(order.status==='PAID')state.className='paid';box.append(title,state);if(order.payment_url&&order.status!=='PAID'){const url=new URL(order.payment_url,location.origin);if(url.origin===location.origin&&url.pathname==='/payments/payhere'){const link=document.createElement('a');link.href=url.href;link.className='primary';link.textContent='Pay with PayHere (sandbox)';box.append(link)}}container.append(box)}
 if(paymentReturn&&(!returnConversation||session===returnConversation)){
  $('payment-banner').hidden=false;const returnedOrders=(data.orders||[]).filter(o=>!returnOrder||o.order_id===returnOrder);const paid=returnedOrders.length>0&&returnedOrders.every(o=>o.status==='PAID');
  $('payment-banner').textContent=paid?'Payment confirmed. Your order and conversation are saved.':paymentReturn==='cancelled'?'You returned from checkout. Your conversation is saved; see the verified order status below.':'Welcome back. Checking PayHere’s verified payment status. Your conversation is saved.';
 }
}
async function openConversation(id){if(busy)return;setBusy(true);clearTimeout(pollTimer);try{const data=await api('/api/conversations/'+encodeURIComponent(id));setSession(id);renderMessages(data);renderCheckout(data);await refreshHistory();pollCount=0;schedulePoll(data)}finally{setBusy(false)}}
function schedulePoll(data){clearTimeout(pollTimer);if(pollCount>=24||!user)return;if(!data.pending&&!(data.orders||[]).some(o=>o.status==='PENDING'))return;pollTimer=setTimeout(async()=>{if(busy){schedulePoll(data);return}const id=session;try{const updated=await api('/api/conversations/'+encodeURIComponent(id));if(id!==session||busy)return;renderCheckout(updated);if(JSON.stringify(updated.messages||[])!==renderedMessages)renderMessages(updated);pollCount++;schedulePoll(updated)}catch(e){showError(e)}},5000);}
async function startNew(){if(busy)return;setBusy(true);clearTimeout(pollTimer);try{const data=await api('/api/conversations',{});setSession(data.session_id);$('payment-banner').hidden=true;renderMessages({messages:[]});renderCheckout({});$('status').textContent='';await refreshHistory();$('question').focus()}finally{setBusy(false)}}
async function submitChat(message,action=null){
 if(busy||!user)return;setBusy(true);$('status').textContent='Working…';
 try{
  if(!session){const created=await api('/api/conversations',{});setSession(created.session_id)}
  add(message||(action==='confirm'?'Confirm order':'Cancel checkout'),'user');$('question').value='';
  const data=await api('/api/chat',{message,action,session_id:session,request_id:crypto.randomUUID()});
  renderMessages(data);renderCheckout(data);$('status').textContent='';await refreshHistory();pollCount=0;schedulePoll(data);
 }catch(e){showError(e);$('question').value=message;if(user&&session){try{const saved=await api('/api/conversations/'+encodeURIComponent(session));renderMessages(saved);renderCheckout(saved);schedulePoll(saved)}catch{}}}
 finally{setBusy(false);$('question').focus();}
}
async function loadAccount(){
 $('login-panel').hidden=true;$('loading').hidden=false;$('account').hidden=true;$('app').hidden=true;$('account-name').textContent=user.name;
 const conversations=await refreshHistory();const selected=new URLSearchParams(location.search).get('conversation')||conversations[0]?.id;
 if(selected){try{await openConversation(selected)}catch(e){showError(e);session=null;renderMessages({messages:[]});$('checkout').replaceChildren();history.replaceState(null,'','/')}}else{session=null;renderMessages({messages:[]})}
 $('loading').hidden=true;$('account').hidden=false;$('app').hidden=false;
}
$('login-form').onsubmit=async e=>{e.preventDefault();$('login-button').disabled=true;$('login-status').textContent='Signing in…';try{const data=await api('/api/auth/login',{username:$('username').value.trim(),password:$('password').value});user=data.user;$('password').value='';$('login-status').textContent='';await loadAccount()}catch(error){$('login-status').textContent=error.message}finally{$('login-button').disabled=false}};
$('logout').onclick=async()=>{try{await api('/api/auth/logout',{});session=null;history.replaceState(null,'','/');showLogin()}catch(e){showError(e)}};
$('new-chat').onclick=()=>startNew().catch(showError);
$('chat-form').onsubmit=e=>{e.preventDefault();const message=$('question').value.trim();if(message)submitChat(message)};
$('question').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$('chat-form').requestSubmit()}};
for(const b of document.querySelectorAll('.prompts button'))b.onclick=()=>submitChat(b.textContent);
window.addEventListener('focus',()=>{if(user&&session&&!busy){const id=session;pollCount=0;api('/api/conversations/'+encodeURIComponent(id)).then(data=>{if(id!==session||busy)return;if(JSON.stringify(data.messages||[])!==renderedMessages)renderMessages(data);renderCheckout(data);schedulePoll(data)}).catch(showError)}});
(async()=>{try{const data=await api('/api/auth/me');user=data.user;await loadAccount()}catch(e){showLogin();if(!e.message.includes('Sign in'))$('login-status').textContent=e.message}})();
