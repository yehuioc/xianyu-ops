import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from console import commerce, publishing as pub, listing_edits as edits
from console.app import create_app
from console.service import ConsoleService
from console.store import Store, product_key
from console.marketplace import EDIT_API, EDIT_DETAIL_API, MarketError

TEMP = Path(__file__).resolve().parents[1] / 'data/test-tmp'
TEMP.mkdir(parents=True, exist_ok=True)


class ListingEditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'store.sqlite3')
        self.store.put('account', 'a', {'id':'a','platform_user_id':'123'}, account='a')
        self.store.save_cookie('a', 'unb=123; _m_h5_tk=test_9999999999')
        source = Path(self.temp.name) / 'listing.json'
        source.write_text(json.dumps({'title':'新版简历工具包','description':'含完整虚构案例和空白表格','images':['主图.png','示例.png']}), encoding='utf-8')
        self.bundle = {'delivery_zip_sha256':'a'*64}
        self.info = {'name':'主图.png','sha256':'b'*64,'width':1200,'height':1200,'mime':'image/png'}
        for item in (patch.object(commerce,'offer',return_value={'sale_type':'digital'}),
                     patch.object(commerce,'source_file',return_value=source),
                     patch.object(commerce,'build_bundles',side_effect=lambda *args:self.bundle),
                     patch.object(pub,'image_bytes',side_effect=lambda slug,name:(b'png',dict(self.info,name=name)))):
            item.start(); self.addCleanup(item.stop)
        self.preview = {'slug':'career-kit','user_id':'123','title':'旧简历工具包','description':'旧介绍','price_cents':1900,
                        'quantity':9999,'address':{'area':'区','divisionId':'440703'},'category':{'channelCatId':'100'},
                        'unique_code':'original','images':[self.info]}
        self.old_image = {'url':'https://img.alicdn.com/old.png','widthSize':1200,'heightSize':1200}
        self.remote = dict(pub.payload(self.preview,[self.old_image]), itemId='222',userId='0',itemStatus='0')
        self.remote['itemAddrDTO']['gps']='private-coordinate'
        self.remote['userRightsProtocols']=[{'name':'keep-protocol','enable':'true'}]
        pub.save(self.store, {'id':'original','account':'a','slug':'career-kit','item_id':'222','state':'published',
                            'preview':copy.deepcopy(self.preview),'uploaded_images':[self.old_image]})
        self.store.put('quark_binding','a:career-kit',{'item_id':'222','sha256':'a'*64},account='a')
        self.store.put('product','a:222',{'account':'a','item_id':'222','managed':True,'watch':False},account='a')
        self.calls = []; self.fail = None; self.after_upload = None
        owner = self
        class Client:
            def __init__(self,*args): pass
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def products(self): return [{'item_id':'222'}]
            async def _post_mtop(self,**kw):
                owner.calls.append(kw)
                if kw['api_name']==EDIT_DETAIL_API: return {'data':copy.deepcopy(owner.remote)}
                if kw['api_name']==EDIT_API:
                    owner.remote.update(copy.deepcopy(kw['payload']))
                    if owner.fail: raise owner.fail
                    return {'data':{'itemId':'222'}}
                raise AssertionError(kw['api_name'])
        self.client = Client

    async def upload(self,client,raw,info):
        if self.after_upload: self.after_upload()
        return {'url':'https://img.alicdn.com/new-'+info['name'],'widthSize':1200,'heightSize':1200}

    def prepare(self,values=None):
        return asyncio.run(edits.prepare(self.store,'a','career-kit',values,client_factory=self.client))

    def apply(self,preview):
        return asyncio.run(edits.apply(self.store,'a','career-kit',preview['id'],preview['digest'],client_factory=self.client,uploader=self.upload))

    def writes(self):
        return [c for c in self.calls if c['api_name']==EDIT_API]

    def test_success_preserves_price_stock_location_and_protocol_then_inventory_works(self):
        before = copy.deepcopy(self.remote)
        preview = self.prepare()
        self.assertNotIn('private-coordinate', json.dumps(preview))
        row = self.apply(preview)
        self.assertEqual(row['state'],'verified')
        self.assertTrue(all(edits.preserved(before,self.remote).values()))
        self.assertEqual(self.remote['itemAddrDTO']['gps'],'private-coordinate')
        record = pub.current(self.store,'a','career-kit')
        self.assertEqual(record['preview'],self.preview)
        self.assertEqual(pub.effective_content(record)[0]['title'],'新版简历工具包')
        self.assertTrue(self.store.get('product','a:222')['managed'])
        self.assertFalse(self.writes()[0]['_refresh_token_once'])
        self.assertTrue(self.writes()[0]['_content_authorized'])
        self.assertEqual(self.apply(preview)['id'],row['id'])
        self.assertEqual(len(self.writes()),1)
        result=asyncio.run(pub.update_inventory(self.store,'a','career-kit',9876,client_factory=self.client))
        self.assertEqual(result['inventory_update']['state'],'verified')
        self.assertEqual(self.remote['itemTextDTO']['title'],'新版简历工具包')
        asyncio.run(edits.reconcile(self.store,'a','career-kit',client_factory=self.client))
        self.assertEqual(pub.effective_content(pub.current(self.store,'a','career-kit'))[0]['quantity'],9876)

    def test_changed_during_upload_stops_before_edit(self):
        preview = self.prepare()
        self.after_upload=lambda:self.remote.update(quantity='9998')
        self.assertEqual(self.apply(preview)['state'],'failed_before_send')
        self.assertEqual(self.writes(),[])

    def test_uncertain_write_reconciles_without_resend_and_blocks_other_edits(self):
        preview = self.prepare()
        self.fail=MarketError('NETWORK_ERROR','timeout')
        self.assertEqual(self.apply(preview)['state'],'unknown')
        for call in (lambda:self.apply(preview),self.prepare,
                     lambda:asyncio.run(pub.update_inventory(self.store,'a','career-kit',5,client_factory=self.client)),
                     lambda:asyncio.run(pub.reconcile(self.store,'a','career-kit',client_factory=self.client))):
            with self.assertRaises(ValueError):call()
        result=asyncio.run(edits.reconcile(self.store,'a','career-kit',client_factory=self.client))
        self.assertEqual(result['state'],'verified')
        self.assertEqual(len(self.writes()),1)

    def test_delivery_and_image_changes_require_fresh_preview(self):
        preview=self.prepare(); self.bundle={'delivery_zip_sha256':'c'*64}
        with self.assertRaisesRegex(ValueError,'交付内容'):self.apply(preview)
        self.bundle={'delivery_zip_sha256':'a'*64}; self.info['sha256']='c'*64
        with self.assertRaisesRegex(ValueError,'图片'):self.apply(preview)
        self.assertEqual(self.writes(),[])

    def test_binding_and_owner_sku_remote_edit_mismatch_fail_closed(self):
        self.store.put('quark_binding','a:career-kit',{'item_id':'another','sha256':'a'*64},account='a')
        with self.assertRaisesRegex(ValueError,'发货'):self.prepare()
        self.store.put('quark_binding','a:career-kit',{'item_id':'222','sha256':'a'*64},account='a')
        self.remote['itemSkuList']=[{'quantity':'1'}]
        with self.assertRaisesRegex(ValueError,'单规格'):self.prepare()
        self.remote.pop('itemSkuList'); self.remote['userId']='999'
        with self.assertRaises(ValueError):self.prepare()
        self.remote['userId']='0'; self.remote['itemTextDTO']['title']='phone edit'
        with self.assertRaises(ValueError):self.prepare()
        with self.assertRaises(ValueError):self.prepare({'price_cents':1})
        self.assertEqual(self.writes(),[])

    def test_old_inventory_baseline_is_archived_after_text_update(self):
        record=pub.current(self.store,'a','career-kit')
        record['inventory_update']={'id':'stock-1','state':'verified','quantity':9999,'before':{k:self.remote.get(k) for k in pub.INVENTORY_FIELDS}}
        pub.save(self.store,record)
        self.assertEqual(self.apply(self.prepare())['state'],'verified')
        self.assertNotIn('inventory_update',pub.current(self.store,'a','career-kit'))
        self.assertIsNotNone(self.store.get('publication_inventory_history','stock-1'))
        result=asyncio.run(pub.reconcile(self.store,'a','career-kit',client_factory=self.client))
        self.assertEqual(result['state'],'published')

    def test_restart_keeps_uncertainty_and_sanitized_result(self):
        preview=self.prepare(); self.fail=MarketError('NETWORK_ERROR','timeout')
        row=self.apply(preview); row['state']='sending'; edits._save(self.store,row)
        edits.recover(self.store)
        row=edits.current(self.store,'a','career-kit')
        self.assertEqual(row['state'],'unknown')
        self.assertNotIn('private-coordinate',json.dumps(edits.public_result(row)))

    def test_api_requires_exact_review_and_account(self):
        service=ConsoleService(self.store,import_legacy=False)
        self.addCleanup(service.close)
        client=TestClient(create_app(service)); preview=self.prepare()
        path='/api/commerce/career-kit/content-update?account=a'
        body={'preview_id':preview['id'],'digest':preview['digest'],'acknowledgment':'wrong'}
        self.assertEqual(client.post(path,json=body).status_code,400)
        body['acknowledgment']='update_this_reviewed_listing_content'; body['digest']='wrong'
        self.assertEqual(client.post(path,json=body).status_code,400)
        body['digest']=preview['digest']
        with patch.object(service,'submit',return_value={'state':'queued'}) as submit:
            self.assertEqual(client.post(path,json=body).status_code,200)
            submit.assert_called_once_with('edit_listing','a','career-kit',preview_id=preview['id'],approved_digest=preview['digest'])


if __name__=='__main__': unittest.main()
