import unittest
from unittest.mock import patch
import operations_intake as o

class ProjectIntakeTests(unittest.TestCase):
    def test_explicit_project_without_pid_or_street(self):
        message={'subject':'Fw: [PROJECT] Example subdivision expansion','body':{'content':'Spectrum assignment\nArea: Florence\nPlease begin the multi-day splicing work.'}}
        with patch.object(o.graph,'parse_recognized_ntp',return_value=None),patch.object(o.graph,'enrich_ntp_from_attachments',side_effect=lambda p,a:p),patch.object(o.graph,'_candidate_document_texts',return_value=[]):
            item=o.parse(message,[])
        self.assertEqual(item['type'],'Project')
        self.assertEqual(item['market'],'Florence')
        self.assertEqual(item['address'],'Example subdivision expansion')
        self.assertEqual(item['pid'],'')
        self.assertNotIn('total',item)

    def test_existing_categories_and_ntp_project_id_are_unchanged(self):
        for subject,expected in [('No light at 100 Example Rd','Outage'),('CRQ 12345 low light repair','CRQ'),('Project ID 1234567 - normal work order','Standard'),('PO Number for NTP 1234 PRISM 1234567 Created/Revised','Standard')]:
            self.assertEqual(o.category_for_subject(subject),expected)

    def test_explicit_project_wins_over_old_outage_word_in_title(self):
        self.assertEqual(o.category_for_subject('[PROJECT] Permanent replacement after outage'),'Project')
        self.assertEqual(o.category_for_message('Example expansion','Category: Project\nArea: Columbia'),'Project')

    def test_project_does_not_absorb_unrelated_small_job_at_same_name(self):
        class Store:
            def get_sheet(self,sid,force=False):
                return {'columns':[{'id':1,'title':'Task Name'},{'id':2,'title':'Portal Job Type'}],'rows':[{'id':7,'cells':[{'columnId':1,'value':'Example subdivision expansion'},{'columnId':2,'value':'Standard'}]}]} if sid==o.AREA['Florence'] else {'columns':[],'rows':[]}
        with self.assertRaisesRegex(ValueError,'separate'):
            o.locate(Store(),{'pid':'','wo':'','address':'Example subdivision expansion','city':'','type':'Project'})

    def test_same_project_matches_existing_record(self):
        class Store:
            def get_sheet(self,sid,force=False):
                return {'columns':[{'id':1,'title':'Task Name'},{'id':2,'title':'Portal Job Type'}],'rows':[{'id':7,'cells':[{'columnId':1,'value':'Example subdivision expansion'},{'columnId':2,'value':'Project'}]}]} if sid==o.AREA['Florence'] else {'columns':[],'rows':[]}
        found=o.locate(Store(),{'pid':'','wo':'','address':'Example subdivision expansion','city':'','type':'Project'})
        self.assertEqual(found[2]['id'],7)

if __name__=='__main__':unittest.main()
