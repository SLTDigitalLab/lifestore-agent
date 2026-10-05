const { chromium }=require('playwright');
const fs=require('fs');
fs.mkdirSync('.test-artifacts',{recursive:true});
if(!process.env.LIFESTORE_TEST_BASE_URL)throw Error('Set LIFESTORE_TEST_BASE_URL to your sandbox HTTPS origin');
if(!process.env.LIFESTORE_TEST_PASSWORD)throw Error('Set LIFESTORE_TEST_PASSWORD for the private tester');
(async()=>{
 const browser=await chromium.launch({...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}),headless:true});
 const context=await browser.newContext({viewport:{width:1360,height:960}});
 const page=await context.newPage();
 page.setDefaultTimeout(30000);
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 try{
  await page.goto(process.env.LIFESTORE_TEST_BASE_URL,{waitUntil:'networkidle'});
  await page.locator('#login-panel').waitFor({state:'visible'});
  await page.screenshot({path:'.test-artifacts/login.png',fullPage:true});
  await page.locator('#username').fill('tester');await page.locator('#password').fill(process.env.LIFESTORE_TEST_PASSWORD);
  await page.locator('#login-button').click();await page.locator('#app').waitFor({state:'visible'});
  const previousURL=page.url();
  await page.locator('#new-chat').click();
  await page.waitForFunction(old=>location.href!==old&&!document.getElementById('send').disabled,previousURL);
  const conversationURL=page.url();
  console.log('SIGNED_IN_AND_NEW_CHAT',conversationURL);
  const question='I want to buy exactly one CUDY WU1400 AC1300 Wi-Fi High Gain Adaptor. I know it is a USB adapter for one computer. Add one to my cart and prepare checkout. My name is Test Customer, phone 0771234567, email test@example.com, delivery address 12 Test Road, Colombo.';
  await page.locator('#question').fill(question);
  const reply=page.waitForResponse(r=>r.url().endsWith('/api/chat')&&r.request().method()==='POST',{timeout:120000});
  await page.locator('#send').click(); const response=await reply;if(response.status()!==200)throw Error('Chat HTTP '+response.status());
  await page.getByRole('button',{name:'Confirm order',exact:true}).waitFor({state:'visible',timeout:120000});
  await page.reload({waitUntil:'networkidle'});
  await page.getByRole('button',{name:'Confirm order',exact:true}).waitFor({state:'visible'});
  if(!(await page.locator('#messages').innerText()).includes('I want to buy exactly one'))throw Error('Chat missing after refresh');
  console.log('REFRESH_RESTORED_MESSAGES_AND_CHECKOUT');
  await page.locator('#logout').click();await page.locator('#login-panel').waitFor({state:'visible'});
  await page.locator('#username').fill('tester');await page.locator('#password').fill(process.env.LIFESTORE_TEST_PASSWORD);await page.locator('#login-button').click();
  await page.locator('#app').waitFor({state:'visible'});
  await page.goto(conversationURL,{waitUntil:'networkidle'});
  await page.getByRole('button',{name:'Confirm order',exact:true}).waitFor({state:'visible'});
  console.log('SIGN_OUT_SIGN_IN_RESTORED_CHAT');
  await page.screenshot({path:'.test-artifacts/restored-checkout.png',fullPage:true});
  await page.getByRole('button',{name:'Confirm order',exact:true}).click();
  await page.getByRole('link',{name:'Pay with PayHere (sandbox)',exact:true}).waitFor({state:'visible'});
  await page.getByRole('link',{name:'Pay with PayHere (sandbox)',exact:true}).click();
  await page.waitForURL('https://sandbox.payhere.lk/**',{timeout:60000});
  await page.waitForLoadState('networkidle');
  console.log('PAYHERE_URL',page.url());console.log('PAYHERE_TEXT',(await page.locator('body').innerText()).slice(0,4500));
  await page.screenshot({path:'.test-artifacts/payhere-before-payment.png',fullPage:true});
  console.log('PAYMENT_READY: proceeding with official sandbox test card');
  if(!await page.getByPlaceholder('Name on Card',{exact:true}).isVisible())await page.locator('#payment_container_VISA').click();
  let cardPage=null;
  const frameDeadline=Date.now()+15000;
  while(!cardPage&&Date.now()<frameDeadline){
   for(const frame of page.frames()){if(await frame.getByPlaceholder('Name on Card',{exact:true}).count()){cardPage=frame;break;}}
   if(!cardPage)await page.waitForTimeout(250);
  }
  if(!cardPage)throw Error('Sandbox card frame not found');
  await cardPage.getByPlaceholder('Name on Card',{exact:true}).fill('Test Customer');
  await cardPage.getByPlaceholder('Credit Card Number',{exact:true}).fill('4916217501611292');
  await cardPage.getByPlaceholder('CVV',{exact:true}).fill('123');
  await cardPage.getByPlaceholder('Expiry MM/YY',{exact:true}).fill('12/28');
  let payButton=null;
  for(const frame of page.frames()){
   const candidate=frame.getByRole('button',{name:/^(Pay|Submit)$/i});
   if(await candidate.count()){payButton=candidate.first();break;}
  }
  if(!payButton){
   for(const frame of page.frames())console.log('FRAME_CONTROLS',new URL(frame.url()||'about:blank').pathname,JSON.stringify(await frame.locator('button,input[type=submit],input[type=button],a,[onclick]').evaluateAll(es=>es.filter(e=>e.getClientRects().length).map(e=>({tag:e.tagName,id:e.id,role:e.getAttribute('role'),type:e.getAttribute('type'),text:e.textContent.trim().slice(0,70),value:e.getAttribute('value')})))));
   throw Error('Pay control not located');
  }
  await payButton.click();
  console.log('SANDBOX_PAYMENT_SUBMITTED');
  try{await page.waitForURL('https://lifestoreai.tharinduwithanage.me/**',{timeout:60000});}
  catch{console.log('GATEWAY_AFTER_PAYMENT',(await page.locator('body').innerText()).slice(0,4500));await page.screenshot({path:'.test-artifacts/payhere-after-payment.png',fullPage:true});throw Error('Gateway has not returned automatically');}
  await page.locator('#app').waitFor({state:'visible'});
  await page.getByText(/Payment confirmed/).first().waitFor({state:'visible',timeout:120000});
  if(new URL(page.url()).searchParams.get('conversation')!==new URL(conversationURL).searchParams.get('conversation'))throw Error('Wrong return conversation');
  if(!(await page.locator('#messages').innerText()).includes('I want to buy exactly one'))throw Error('Return lost history');
  await page.reload({waitUntil:'networkidle'});await page.getByText(/Payment confirmed/).first().waitFor({state:'visible'});
  const receipts=page.locator('#messages .assistant').filter({hasText:'Your payment of Rs.'});
  if(await receipts.count()!==1)throw Error('Expected one saved assistant payment receipt');
  await page.screenshot({path:'.test-artifacts/payment-return-success.png',fullPage:true});
  console.log('PASS: live AI, checkout, actual sandbox payment, correct return, verified PAID, refresh, logout/login persistence');
  if(errors.length)throw Error('Browser errors: '+errors.join(';'));
 }catch(error){console.error('BROWSER_TEST_FAILED',error.message);console.log('URL',page.url());console.log('PAGE_TEXT',(await page.locator('body').innerText()).slice(0,5000));await page.screenshot({path:'.test-artifacts/failure.png',fullPage:true});process.exitCode=1;}
 finally{await browser.close();}
})();
