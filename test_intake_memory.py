"""Intake reads photo metadata once and retains actual photos from source MIME."""
import unittest
from email.message import EmailMessage
from unittest.mock import patch
import graph_import as graph
import operations_intake as intake

class IntakeMemoryTests(unittest.TestCase):
    def test_inline_bytes_not_downloaded_twice_but_documents_are_loaded(self):
        root=graph.GRAPH_ROOT+'/users/fictional/messages/message/attachments'
        calls=[]
        def request(method,url,**kwargs):
            calls.append(url)
            if '$select=' in url:return {'value':[{'id':'photo','name':'photo.jpg','contentType':'image/jpeg','isInline':True,'size':3000000}],'@odata.nextLink':root+'?$skip=1'}
            if '$skip=' in url:return {'value':[{'id':'plan','name':'plan.pdf','contentType':'application/pdf','isInline':False,'size':500}]}
            if url.endswith('/plan'):return {'@odata.type':'#microsoft.graph.fileAttachment','name':'plan.pdf','contentType':'application/pdf','contentBytes':'cGxhbg=='}
            raise AssertionError('Unexpected binary photo fetch')
        with patch.object(graph,'mailbox',return_value='fictional'),patch.object(graph,'_request',side_effect=request):
            files=graph.list_attachments('fake','message',inline_content=False)
        self.assertNotIn('contentBytes',files[0]);self.assertEqual(graph.attachment_bytes(files[1])[2],b'plan');self.assertEqual(len(calls),3)

    def test_all_inline_work_photos_still_saved_from_mime(self):
        message=EmailMessage();message.set_content('Work update')
        message.add_alternative('<p>New field picture</p><img src="cid:photo">',subtype='html')
        message.get_payload()[1].add_related(b'fictional-work-photo',maintype='image',subtype='jpeg',cid='<photo>',filename='image001.jpg')
        raw=message.as_bytes()
        class Store:
            def __init__(self):self.files=[]
            def list_row_attachments(self,*args):return [{'name':f[0]} for f in self.files]
            def attach_file_to_row(self,*args,filename,mime_type,data):self.files.append((filename,data))
        store=Store();metadata=[{'@odata.type':'#microsoft.graph.fileAttachment','name':'image001.jpg','contentType':'image/jpeg','isInline':True,'size':3000000}]
        with patch.object(graph,'get_message_mime',return_value=raw):
            for _ in range(2):intake.capture_email_files(store,'fake',{'id':'fixture'},1,2,metadata,'a'*64)
        self.assertEqual(len(store.files),2)
        self.assertEqual({data for name,data in store.files},{raw,b'fictional-work-photo'})

    def test_doc_recognition_does_not_decode_photo_bytes(self):
        with patch.object(graph,'attachment_bytes',side_effect=AssertionError('Photos need no document decoding')):
            self.assertEqual(graph._candidate_document_texts([{'name':'photo.jpg','contentType':'image/jpeg','contentBytes':'large-payload'}]),[])

    def test_default_attachment_behavior_is_unchanged(self):
        with patch.object(graph,'mailbox',return_value='fictional'),patch.object(graph,'_request',return_value={'value':[{'name':'photo.jpg','contentBytes':'aW1hZ2U='}]}):
            self.assertIn('contentBytes',graph.list_attachments('fake','message')[0])

if __name__=='__main__':unittest.main()
