import unittest
from unittest.mock import patch
import graph_import as graph
import operations_intake as intake
import intake_formats as formats

class EmptyStore:
    def get_sheet(self,*args,**kwargs):return {'columns':[],'rows':[]}

class IntakeFormatTests(unittest.TestCase):
    def test_push_light_reads_ntp_before_rejecting_subject(self):
        m={'subject':'Fw: [EXTERNAL] Push Light-901 Fictional Ave N-7A999E','body':{'content':'Spectrum dispatch'}}
        ntp=graph.ParsedNtp('3999991','7999991',m['subject'],address='901 Fictional Hwy N',city='Jackson',due_date='2026-10-27')
        with patch.object(graph,'enrich_ntp_from_attachments',return_value=ntp),patch.object(graph,'_candidate_document_texts',return_value=[]):
            result=intake.parse(m,[])
        self.assertEqual(result['address'],'901 Fictional Hwy N')
        self.assertEqual(result['market'],'Columbia');self.assertEqual(intake.internal_due(result['ntp_due']),'2026-10-26')

    def test_crq_where_when_and_full_state_name(self):
        m={'subject':'Fw: CRQ009999991111','body':{'content':'Spectrum\nWhen: Tue 11/3/2026 12:00 AM - 6:00 AM\nWhere: 901 Fictional Ave (901 Fictional Ave, McBee, South Carolina 29101)\nWhen: Wed 10/21/2026 12:00 AM'}}
        with patch.object(graph,'_candidate_document_texts',return_value=[]):result=intake.parse(m,[])
        self.assertEqual((result['address'],result['city'],result['market']),('901 Fictional Ave','Mcbee','Florence'))
        self.assertEqual(result['crq_id'],'CRQ009999991111');self.assertEqual(result['planned_date'],'2026-11-03')

    def test_written_appointment_date(self):
        self.assertEqual(formats.appointment('When: Tuesday, November 3, 2026 12:00 AM')['planned_date'],'2026-11-03')

    def test_trusted_no_light_dispatch_without_spectrum_signature(self):
        m={'subject':'Fw: no light/ 7B999E/ 901 fictional ave barnwell sc 29812','from':{'emailAddress':{'address':'dispatch@graycliffent.com'}},'body':{'content':'Dispatch work files attached'}}
        with patch.object(graph,'_candidate_document_texts',return_value=[]):result=intake.message_item(EmptyStore(),m,[])
        self.assertEqual((result['type'],result['market']),('Outage','Columbia'))

    def test_trusted_forward_header_and_untrusted_sender(self):
        m={'subject':'no light 7B999E 901 Fictional Ave Barnwell SC 29812','from':{'emailAddress':{'address':'forwarder@firstdigitalsc.com'}}}
        self.assertTrue(formats.trusted_no_light(m,'From: Dispatch <dispatch@graycliffent.com>'))
        m['from']['emailAddress']['address']='other@example.invalid'
        self.assertFalse(formats.trusted_no_light(m,'From: Dispatch <dispatch@graycliffent.com>'))

    def test_mixed_customer_scope_still_requires_review(self):
        with patch.object(graph,'_candidate_document_texts',return_value=[]):
            with self.assertRaisesRegex(ValueError,'mixed'):
                intake.spectrum_scope({'subject':'no light 7B999E 901 Fictional Ave Barnwell SC 29812','from':{'emailAddress':{'address':'dispatch@graycliffent.com'}},'body':{'content':'Kinetic assignment'}},[])

    def test_structured_node_dispatch_and_survey_complete(self):
        self.assertEqual(intake.category_for_message('NODE ::: 2F999E - 901 Fictional Dr','No light at MST. Sent to Graycliff to fix. Construction Survey Completed Yes'),'Outage')
        self.assertFalse(formats.resolved_incident('Construction Survey Completed Yes'))
        self.assertTrue(formats.resolved_incident('Service restored. Hands off.'))

    def test_crq_exact_id_links_a_renamed_job(self):
        class Store(EmptyStore):
            def get_sheet(self,sid,**kw):
                if sid!=intake.AREA['Florence']:return super().get_sheet(sid,**kw)
                return {'columns':[{'id':1,'title':'Task Name'},{'id':2,'title':'Notes'}],'rows':[{'id':99,'cells':[{'columnId':1,'value':'Cell tower migration'},{'columnId':2,'value':'CRQ009999991111'}]}]}
        result=intake.locate(Store(),{'pid':'','wo':'','address':'','city':'','type':'CRQ','crq_id':'CRQ009999991111'})
        self.assertEqual(result[2]['id'],99)

    def test_parent_recovery_is_exact_and_preserves_original(self):
        m={'id':'reply','subject':'Re: Push Light-901 Fictional St Ext-7A999E','from':{'emailAddress':{'address':'forwarder@firstdigitalsc.com'}}}
        parent={'id':'parent','subject':'Fw: Push Light-901 Fictional St Ext-7A999E','internetMessageId':'<fictional-parent@example.invalid>'}
        files=[{'name':'NTP.pdf'}]
        def docs(items):return [('NTP.pdf','PRISM ID: 7999991 Work Order #: 3999991')] if items else []
        with patch.object(graph,'_candidate_document_texts',side_effect=docs),patch.object(graph,'get_message_mime',return_value=b'In-Reply-To: <fictional-parent@example.invalid>\r\n\r\n'),patch.object(graph,'_request',side_effect=[{'value':[parent]},{'value':files},b'Original source email']):
            result=formats.enrich_push_parent('fake',m,[])
        self.assertEqual(result[0],files[0]);self.assertTrue(result[-1]['name'].endswith('.eml'))

    def test_parent_location_conflict_is_held(self):
        m={'id':'reply','subject':'Re: Push Light-901 Fictional St Ext-7A999E','from':{'emailAddress':{'address':'forwarder@firstdigitalsc.com'}}}
        parent={'id':'parent','subject':'Push Light-902 Fictional St Ext-7A999E','internetMessageId':'<fictional-parent@example.invalid>'}
        with patch.object(graph,'_candidate_document_texts',return_value=[]),patch.object(graph,'get_message_mime',return_value=b'In-Reply-To: <fictional-parent@example.invalid>\r\n\r\n'),patch.object(graph,'_request',return_value={'value':[parent]}):
            with self.assertRaisesRegex(ValueError,'location conflicts'):formats.enrich_push_parent('fake',m,[])

    def test_older_source_cannot_replace_newer_window(self):
        self.assertFalse(formats.newer_source('2026-10-20T19:45:00Z','2026-10-21T14:04:00+00:00'))
        self.assertTrue(formats.newer_source('2026-10-21T14:04:00Z','2026-10-20T19:45:00+00:00'))

    def test_multiple_ntps_do_not_create_an_ambiguous_job(self):
        docs=[('NTP-A.pdf','PRISM ID: 7999991 Work Order #: 3999991'),('NTP-B.pdf','PRISM ID: 7999992 Work Order #: 3999992')]
        with patch.object(graph,'_candidate_document_texts',return_value=docs):
            with self.assertRaisesRegex(ValueError,'Multiple NTP jobs'):
                intake.parse({'subject':'Push Light-901 Fictional St-7A999E','body':{'content':'Spectrum'}},[])

    def test_different_crq_at_same_address_is_not_silently_merged(self):
        class Store(EmptyStore):
            def get_sheet(self,sid,**kw):
                if sid!=intake.AREA['Florence']:return super().get_sheet(sid,**kw)
                return {'columns':[{'id':1,'title':'Task Name'},{'id':2,'title':'Portal CRQ ID'}],'rows':[{'id':99,'cells':[{'columnId':1,'value':'901 Fictional Ave'},{'columnId':2,'value':'CRQ009999991111'}]}]}
        with self.assertRaisesRegex(ValueError,'different CRQ'):
            intake.locate(Store(),{'pid':'','wo':'','address':'901 Fictional Ave','city':'','type':'CRQ','crq_id':'CRQ009999992222'})

    def test_branded_signature_keeps_body_photo(self):
        self.assertTrue(intake.signature_graphic('unitek-graphic-esign26_00000000-0000-0000-0000-000000000000.jpeg','image/jpeg',b'branded signature'))
        self.assertFalse(intake.signature_graphic('image001.png','image/png',b'field photo'))

if __name__=='__main__':unittest.main()
