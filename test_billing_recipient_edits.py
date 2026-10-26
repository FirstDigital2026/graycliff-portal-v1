"""Fictional billing previews with mocked transport; never send real email."""
import json,time,unittest
import billing_mail as b
from test_billing_review import UnpricedReviewTests


class BillingRecipientEditTests(UnpricedReviewTests):
    def edit(self, preview, **changes):
        return b.handle(object(),self.db,{**self.payload,'op':'recipients','id':preview['id'],
            'actor':'reviewer@example.invalid','recipients':{'to':['new@example.invalid'],'cc':[]},**changes})

    def test_edited_recipients_are_saved_and_used_only_on_explicit_send(self):
        original=b.handle(object(),self.db,self.payload)
        edited=self.edit(original)
        self.assertNotEqual(edited['id'],original['id'])
        self.mocks[5].assert_not_called()
        self.assertEqual(self.route['to'],['test@example.invalid'])
        with self.db() as c:
            row=c.execute('SELECT * FROM billing_package_outbox WHERE id=?',(edited['id'],)).fetchone()
            snap=json.loads(row['snapshot'])
            self.assertEqual(snap['sourceRoute']['to'],self.route['to'])
            self.assertEqual(snap['reviewedRecipients']['actor'],'reviewer@example.invalid')
            self.assertEqual(snap['draft']['to'],['new@example.invalid'])
        with self.assertRaisesRegex(ValueError,'Prepare and review'):
            b.handle(object(),self.db,{**self.payload,'op':'send','id':original['id']})
        self.mocks[6].reset_mock()
        sent=b.handle(object(),self.db,{**self.payload,'op':'send','id':edited['id']})
        self.assertEqual(sent['state'],'ACCEPTED')
        draft=self.mocks[6].call_args.args[0]
        self.assertEqual(draft['to'],['new@example.invalid']);self.assertEqual(draft['cc'],[])
        self.assertEqual(draft['parent'],self.route['messageId'])
        b.handle(object(),self.db,{**self.payload,'op':'send','id':edited['id']})
        self.mocks[5].assert_called_once()

    def test_invalid_edits_never_replace_prepared_preview(self):
        d=b.handle(object(),self.db,self.payload)
        invalid=[{'to':[],'cc':[]},{'to':['bad\r\nBcc: injected@example.invalid'],'cc':[]},
                 {'to':[b.mail.SENDER],'cc':[]},{'to':['same@example.invalid'],'cc':['same@example.invalid']},
                 {'to':['a@example.invalid']*21,'cc':[]},{'to':['a@example.invalid'],'cc':[],'bcc':[]}]
        for recipients in invalid:
            with self.assertRaises(ValueError):self.edit(d,recipients=recipients)
        with self.db() as c:self.assertEqual(c.execute('SELECT state FROM billing_package_outbox').fetchone()[0],'PREPARED')
        self.mocks[5].assert_not_called()

    def test_changed_market_policy_or_original_parent_still_blocks_send(self):
        for change in ({'to':['policy-change@example.invalid']},{'messageId':'<new-parent@example.invalid>'}):
            self.mocks[3].return_value=self.route
            d=self.edit(b.handle(object(),self.db,self.payload))
            self.mocks[3].return_value={**self.route,**change}
            with self.assertRaises(ValueError):b.handle(object(),self.db,{**self.payload,'op':'send','id':d['id']})
        self.mocks[5].assert_not_called()

    def test_expired_and_other_job_previews_cannot_be_edited(self):
        d=b.handle(object(),self.db,self.payload)
        with self.assertRaises(ValueError):self.edit(d,rowId='999')
        with self.db() as c:c.execute('UPDATE billing_package_outbox SET created_at=?',(time.time()-1801,))
        with self.assertRaisesRegex(ValueError,'expired'):self.edit(d)
        self.mocks[5].assert_not_called()

    def test_repeated_edit_rotates_preview_and_normalizes_addresses(self):
        d=b.handle(object(),self.db,self.payload)
        edited=self.edit(d,recipients={'to':['NEW@example.invalid','new@example.invalid'],'cc':['copy@example.invalid']})
        self.assertEqual(edited['to'],['new@example.invalid'])
        with self.assertRaises(ValueError):self.edit(d)
        again=self.edit(edited,recipients={'to':['other@example.invalid'],'cc':[]})
        self.assertNotEqual(again['id'],edited['id'])
        self.mocks[5].assert_not_called()


if __name__=='__main__':unittest.main()
