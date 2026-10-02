import asyncio
import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from console import publishing as pub, commerce
from console.app import create_app
from console.service import ConsoleService
from console.store import Store, product_key
from console.marketplace import CATEGORY_API, EDIT_DETAIL_API, PUBLISH_API, EDIT_API, ITEM_LIST_API, MarketError, MtopClient
from public_fixtures import install_catalog

ROOT = Path(__file__).resolve().parents[1]
TEMP = ROOT / 'data/test-tmp'
TEMP.mkdir(parents=True, exist_ok=True)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        install_catalog(self, self.temp.name)
        self.store = Store(Path(self.temp.name) / 'store.sqlite3')
        self.store.put('account', 'a', {'id':'a', 'platform_user_id':'123', 'auth_state':'verified'}, account='a')
        self.store.save_cookie('a','unb=123; _m_h5_tk=test_9999999999')
        self.bundle_patch = patch.object(commerce, 'DATA', Path(self.temp.name))
        self.bundle_patch.start()
        owner = self
        self.calls, self.sent, self.remote = [], None, None
        self.failure, self.upload_failure, self.missing_id = None, False, False
        self.read_failure = False
        self.edit_failure = None
        class Client:
            def __init__(self, *args): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def products(self):
                return [{'item_id':'111','title':'existing'}] + ([{'item_id':'222','title':owner.remote['itemTextDTO']['title']}] if owner.remote else [])
            async def _post_mtop(self, **kw):
                owner.calls.append(kw)
                if kw['api_name'] == CATEGORY_API:
                    return {'data':{'categoryPredictResult':{'catId':'500','channelCatId':'100','catName':'设计素材'}}}
                if kw['api_name'] == EDIT_DETAIL_API:
                    if kw['payload']['itemId'] == '111':
                        return {'data':{'userId':'123','itemAddrDTO':{'prov':'广东','city':'江门','area':'蓬江区','divisionId':'440703','gps':'precise private coordinates','poiName':'private home'}}}
                    if owner.read_failure: raise MarketError('NETWORK_ERROR','read unavailable')
                    return {'data':copy.deepcopy(owner.remote)}
                if kw['api_name'] == PUBLISH_API:
                    owner.sent = kw['payload']
                    owner.remote = dict(copy.deepcopy(owner.sent), userId='123', itemStatus='0', itemId='222')
                    if owner.failure: raise owner.failure
                    return {'data':{} if owner.missing_id else {'itemId':'222'}}
                if kw['api_name'] == EDIT_API:
                    owner.remote.update(copy.deepcopy(kw['payload']))
                    if owner.edit_failure: raise owner.edit_failure
                    return {'data':{'itemId':'222'}}
                raise AssertionError('unrecognized API')
        self.client = Client

    def tearDown(self):
        self.bundle_patch.stop()
        self.temp.cleanup()

    async def uploader(self, client, raw, info):
        if self.upload_failure: raise TimeoutError()
        return {'url':'https://img.alicdn.com/listing.png','widthSize':info['width'],'heightSize':info['height']}

    def preview(self, **values):
        return asyncio.run(pub.prepare(self.store,'a','career-kit',values,client_factory=self.client))

    def run_publish(self, p):
        return asyncio.run(pub.publish(self.store,'a','career-kit',p['id'],p['digest'],client_factory=self.client,uploader=self.uploader))

    def test_success_exact_price_and_readback_no_precise_location(self):
        p = self.preview(price_cents=1990, quantity=3)
        self.assertNotIn('gps',p['address'])
        r = self.run_publish(p)
        self.assertEqual(r['state'],'published')
        self.assertTrue(all(r['checks'].values()))
        self.assertEqual(self.sent['itemPriceDTO']['priceInCent'],'1990')
        self.assertEqual(self.sent['quantity'],'3')
        self.assertEqual(self.sent['itemAddrDTO']['gps'],'')
        self.assertEqual(self.sent['itemAddrDTO']['poiName'],'蓬江区')
        self.assertTrue(self.sent['itemTextDTO']['titleDescSeparate'])
        self.assertFalse(self.store.get('product','a:222')['managed'])
        self.assertEqual(len([c for c in self.calls if c['api_name']==PUBLISH_API]),1)
        with self.assertRaises(ValueError): self.run_publish(p)
        with self.assertRaises(ValueError): self.preview()

    def test_repeatable_stock_defaults_and_inventory_only_round_trip(self):
        self.assertEqual(self.preview()['quantity'],9999)
        self.assertEqual(commerce.default_quantity({'sale_type':'service'}),99)
        p=self.preview(quantity=1); self.run_publish(p)
        before=copy.deepcopy(self.remote)
        r=asyncio.run(pub.update_inventory(self.store,'a','career-kit',9999,client_factory=self.client))
        self.assertEqual(r['inventory_update']['state'],'verified')
        self.assertEqual(r['observed']['quantity'],'9999')
        self.assertEqual(r['preview']['quantity'],1)
        self.assertTrue(all(pub.inventory_preserved(before,self.remote).values()))
        self.assertEqual(r['state'],'published')
        asyncio.run(pub.update_inventory(self.store,'a','career-kit',9999,client_factory=self.client))
        self.assertEqual(len([c for c in self.calls if c['api_name']==EDIT_API]),1)

    def test_category_uses_exact_selected_card_when_prediction_omits_ids(self):
        d={'categoryPredictResult':{'sugShow':'1'},'cardList':[{'cardData':{'propertyId':'-10000','valuesList':[
            {'catId':'50023914','catName':'AI办公工具/服务','channelCatId':'202145854','isClicked':'1'},
            {'catName':'another','channelCatId':'999'}]}}]}
        self.assertEqual(pub.recommended_category(d),{'catId':'50023914','catName':'AI办公工具/服务','channelCatId':'202145854'})
        d['cardList'][0]['cardData']['valuesList'][0].pop('catId')
        with self.assertRaises(ValueError):pub.recommended_category(d)

    def test_inventory_timeout_readback_and_restart_never_resend(self):
        self.run_publish(self.preview(quantity=1))
        self.edit_failure=MarketError('NETWORK_ERROR','timeout')
        r=asyncio.run(pub.update_inventory(self.store,'a','career-kit',9999,client_factory=self.client))
        self.assertEqual(r['inventory_update']['state'],'unknown')
        self.read_failure=True
        with self.assertRaises(MarketError):
            asyncio.run(pub.update_inventory(self.store,'a','career-kit',9999,client_factory=self.client))
        self.read_failure=False
        r=asyncio.run(pub.reconcile(self.store,'a','career-kit',client_factory=self.client))
        self.assertEqual(r['inventory_update']['state'],'verified')
        self.assertEqual(len([c for c in self.calls if c['api_name']==EDIT_API]),1)
        r['inventory_update']['state']='sending'; pub.save(self.store,r)
        pub.recover(self.store)
        self.assertEqual(pub.current(self.store,'a','career-kit')['inventory_update']['state'],'unknown')

    def test_inventory_refuses_sku_and_changed_content(self):
        self.run_publish(self.preview(quantity=1))
        self.remote['itemSkuList']=[{'quantity':'1'}]
        with self.assertRaisesRegex(ValueError,'多规格'):
            asyncio.run(pub.update_inventory(self.store,'a','career-kit',9999,client_factory=self.client))
        self.remote.pop('itemSkuList'); self.remote['itemTextDTO']['title']='changed remotely'
        with self.assertRaises(ValueError):
            asyncio.run(pub.update_inventory(self.store,'a','career-kit',9999,client_factory=self.client))
        self.assertFalse(any(c['api_name']==EDIT_API for c in self.calls))

    def test_lost_response_reconcile_recovers_without_resend(self):
        p = self.preview()
        self.failure = MarketError('NETWORK_ERROR','timeout')
        self.assertEqual(self.run_publish(p)['state'],'unknown')
        pub.recover(self.store)
        with self.assertRaises(ValueError): self.run_publish(p)
        recovered = asyncio.run(pub.reconcile(self.store,'a','career-kit',client_factory=self.client))
        self.assertEqual(recovered['state'],'published')
        self.assertEqual(len([c for c in self.calls if c['api_name']==PUBLISH_API]),1)

    def test_reconcile_preserves_later_user_management_and_watch_settings(self):
        p=self.preview(); self.run_publish(p)
        product=self.store.get('product','a:222')
        product.update(managed=True,watch=True,user_note='keep')
        self.store.put('product','a:222',product,account='a')
        asyncio.run(pub.reconcile(self.store,'a','career-kit',client_factory=self.client))
        saved=self.store.get('product','a:222')
        self.assertTrue(saved['managed']); self.assertTrue(saved['watch'])
        self.assertEqual(saved['user_note'],'keep')

    def test_known_owned_item_reconciles_when_list_is_blocked_but_never_accepts_other_owner(self):
        self.run_publish(self.preview())
        self.store.put('api_block', product_key('a',ITEM_LIST_API), {'code':'FAIL_SYS_USER_VALIDATE'}, account='a')
        async def blocked(*args, **kwargs):
            raise AssertionError('blocked item list must not be retried')
        with patch.object(self.client, 'products', blocked):
            self.remote['userId'] = '0'
            result = asyncio.run(pub.reconcile(self.store,'a','career-kit',client_factory=self.client))
            self.assertEqual(result['state'],'published')
            self.assertEqual(result['ownership_evidence'],'prior_owned_publication_and_current_exact_edit_detail')
            self.remote['userId'] = '999'
            result = asyncio.run(pub.reconcile(self.store,'a','career-kit',client_factory=self.client))
            self.assertEqual(result['state'],'needs_review')
            self.assertFalse(result['checks']['owner'])

    def test_blocked_list_without_previous_owner_proof_cannot_use_cached_item(self):
        self.run_publish(self.preview())
        record = pub.current(self.store,'a','career-kit')
        record['checks']['owner'] = False
        pub.save(self.store, record)
        self.store.put('api_block', product_key('a',ITEM_LIST_API), {'code':'FAIL_SYS_USER_VALIDATE'}, account='a')
        async def blocked(*args, **kwargs):
            raise MarketError('FAIL_SYS_USER_VALIDATE','list blocked')
        with patch.object(self.client, 'products', blocked):
            with self.assertRaises(MarketError):
                asyncio.run(pub.reconcile(self.store,'a','career-kit',client_factory=self.client))

    def test_success_without_id_and_busy_are_never_safe_to_resend(self):
        p = self.preview()
        self.missing_id = True
        self.assertEqual(self.run_publish(p)['state'],'unknown')
        with self.assertRaises(ValueError): self.run_publish(p)

    def test_upload_failure_has_no_publish_call_and_needs_new_review(self):
        p = self.preview()
        self.upload_failure = True
        self.assertEqual(self.run_publish(p)['state'],'failed_before_publish')
        self.assertFalse(any(c['api_name']==PUBLISH_API for c in self.calls))
        with self.assertRaises(ValueError): self.run_publish(p)
        self.upload_failure = False
        self.assertEqual(self.run_publish(self.preview())['state'],'published')

    def test_read_failure_after_ack_never_becomes_safe_failure(self):
        p=self.preview()
        self.read_failure=True
        result=self.run_publish(p)
        self.assertEqual(result['state'],'acknowledged')
        self.assertEqual(result['item_id'],'222')
        with self.assertRaises(ValueError): self.preview()

    def test_double_submit_has_one_atomic_claim_even_across_store_instances(self):
        p=self.preview()
        def attempt(_):
            try:
                pub.claim(Store(self.store.path),'a','career-kit',p['id'],p['digest'])
                return True
            except ValueError: return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(attempt,range(2))).count(True),1)

    def test_restart_after_send_remains_unknown(self):
        p=self.preview()
        r=pub.claim(self.store,'a','career-kit',p['id'],p['digest'])
        r['state']='sending'; pub.save(self.store,r)
        pub.recover(self.store)
        self.assertEqual(pub.current(self.store,'a','career-kit')['state'],'unknown')
        with self.assertRaises(ValueError): self.run_publish(p)

    def test_redacted_owner_requires_authenticated_own_catalog_membership(self):
        p=self.preview()
        self.run_publish(p)
        data=dict(self.remote,userId='0')
        images=pub.current(self.store,'a','career-kit')['uploaded_images']
        self.assertTrue(pub.matches(p,images,data,item_id='222',owned_ids={'222'})['owner'])
        self.assertFalse(pub.matches(p,images,data,item_id='222',owned_ids={'111'})['owner'])
        data['userId']='999'
        self.assertFalse(pub.matches(p,images,data,item_id='222',owned_ids={'222'})['owner'])

    def test_cdn_rewrite_preserves_entire_asset_identity_but_not_other_files(self):
        uploaded='https://img.alicdn.com/imgextra/i4/123/image-abc.png'
        returned='http://img.alicdn.com/bao/uploaded/i4/123/image-abc.png'
        self.assertEqual(pub.image_key(uploaded),pub.image_key(returned))
        self.assertNotEqual(pub.image_key(uploaded),pub.image_key(returned.replace('/123/','/999/')))
        self.assertNotEqual(pub.image_key(uploaded),pub.image_key(returned.replace('image-abc','image-other')))
        self.assertNotEqual(pub.image_key(uploaded),pub.image_key(returned.replace('img.alicdn.com','evil.example')))

    def test_returned_wrong_item_cannot_pass_even_if_all_content_matches(self):
        p=self.preview(); self.run_publish(p)
        images=pub.current(self.store,'a','career-kit')['uploaded_images']
        wrong=dict(self.remote,itemId='333')
        self.assertFalse(pub.matches(p,images,wrong,item_id='222',owned_ids={'222'})['item_id'])

    def test_wrong_account_tampered_review_and_path_traversal_are_rejected(self):
        p=self.preview()
        with self.assertRaises(ValueError): pub.claim(self.store,'a','career-kit',p['id'],'wrong')
        self.store.save_cookie('a','unb=999; _m_h5_tk=test_9999999')
        with self.assertRaises(ValueError): self.run_publish(p)
        with self.assertRaises(ValueError): pub.image_bytes('career-kit','../../../../console/store.py')
        for values in ({'price_cents':True},{'price_cents':19.9},{'quantity':0},{'title':''}):
            with self.assertRaises(ValueError): pub.validate_text(commerce.offer('career-kit'),values)

    def test_changed_material_blocks_publish(self):
        p=self.preview()
        with patch.object(commerce,'build_bundles',return_value={'content_sha256':'changed'}):
            self.assertEqual(self.run_publish(p)['state'],'failed_before_publish')
        self.assertFalse(any(c['api_name']==PUBLISH_API for c in self.calls))

    def test_http_boundary_needs_matching_review_and_explicit_confirmation(self):
        service=ConsoleService(self.store,import_legacy=False)
        try:
            client=TestClient(create_app(service))
            for body in ({},{'acknowledgment':'publish_this_reviewed_listing','preview_id':'missing','digest':'bad'}):
                self.assertEqual(client.post('/api/commerce/career-kit/publish?account=a',json=body).status_code,400)
            self.assertEqual(client.post('/api/commerce/career-kit/publish?account=a',json={},headers={'Origin':'https://evil.example'}).status_code,403)
            self.assertEqual(len(self.store.jobs()),0)
        finally: service.close()

    def test_adapter_rejects_arbitrary_writes_and_unapproved_publish(self):
        async def run():
            async with MtopClient(self.store,'a') as c:
                for kwargs in ({'api_name':PUBLISH_API},{'api_name':'mtop.evil.write','_publish_authorized':True}):
                    with self.assertRaises(MarketError): await c._post_mtop(payload={},**kwargs)
        asyncio.run(run())

    def test_upload_requires_actual_object_wrapper_cdn_url_and_dimensions(self):
        class Response:
            status=200
            value={'object':{'url':'//img.alicdn.com/a.png','pix':'100x200'}}
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def json(self,**kwargs): return self.value
        class Session:
            def post(self,*args,**kwargs): return Response()
        class Client:
            session=Session(); cookies={}; user_agent='test'
        info={'name':'主图.png','mime':'image/png','width':100,'height':200}
        image=asyncio.run(pub.upload(Client(),b'image',info))
        self.assertEqual(image['url'],'https://img.alicdn.com/a.png')
        self.assertEqual(image['heightSize'],200)
        for value in ({'url':'https://img.alicdn.com/a.png'}, {'object':{'url':'https://evil.example/a.png','pix':'1x1'}}, {'object':{'url':'https://img.alicdn.com/a.png','pix':'0x0'}}):
            Response.value=value
            with self.assertRaises(ValueError): asyncio.run(pub.upload(Client(),b'image',info))


if __name__ == '__main__': unittest.main()
