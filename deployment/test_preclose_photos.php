<?php
define('ABSPATH',__DIR__);
function add_action(...$args){}
function register_rest_route(...$args){}
class WP_Error {public $message;function __construct($code,$message,$details){$this->message=$message;}}
function is_wp_error($value){return $value instanceof WP_Error;}
function wp_remote_get($url,$options){$GLOBALS['downloads'][]=$url;return ['body'=>"\xff\xd8".$url];}
function wp_remote_retrieve_response_code($reply){return 200;}
function wp_remote_retrieve_body($reply){return $reply['body'];}
class FDGC_Live_Operations {
 static function mapping(){return ['florence'=>['id'=>'7']];}
 static function sheet(...$args){return ['id'=>'7','rows'=>[['id'=>'123']]];}
 static function job(...$args){return ['key'=>'florence:123','type'=>'Standard'];}
 static function api($method,$path){return ['url'=>'https://example.invalid/'.basename($path)];}
}
class FDGC_Completion_Photos {
 static function row_files(...$args){$out=[];for($i=1;$i<=60;$i++)$out[]=['id'=>(string)$i,'name'=>'photo-'.$i.'.jpg','sizeInKb'=>1];return $out;}
 static function selected(...$args){return true;}
 static function optimize($bytes,$budget){return $bytes;}
}
class FakeDB {public $prefix='test_';function prepare($sql,...$args){return $sql;}function get_var($sql){return json_encode($GLOBALS['event']);}}
$wpdb=new FakeDB();
require __DIR__.'/preclose-email-photos.php';
function expect($condition,$message){if(!$condition)throw new RuntimeException($message);}
function throws($callback){try{$callback();}catch(RuntimeException $error){return;}throw new RuntimeException('Expected selection to be rejected.');}
$available=FDGC_Preclose_Photos::refs(['id'=>'7'],['id'=>'123'],['type'=>'Standard']);
$selection=FDGC_Preclose_Photos::selection(['7:2','7:40'],$available);
expect(count($available)===60&&count($selection['photos'])===2,'Job gallery must preserve all 60 photos while selecting only two.');
throws(fn()=>FDGC_Preclose_Photos::selection(['7:2','7:2'],$available));
throws(fn()=>FDGC_Preclose_Photos::selection(['8:2'],$available));
throws(fn()=>FDGC_Preclose_Photos::selection(array_slice(array_keys($available),0,11),$available));
$event=['committed'=>true,'jobKey'=>'florence:123','status'=>'Field complete','completionPhotoSelection'=>$selection];
class FakeRequest {public $data;function get_json_params(){return $this->data;}}
$request=new FakeRequest();$request->data=['jobKey'=>'florence:123','eventId'=>'saved-event','selection'=>$selection];$downloads=[];
$result=FDGC_Preclose_Photos::package($request);
expect(!is_wp_error($result)&&count($result['photos'])===2,'Only selected photos may be prepared.');
expect($downloads===['https://example.invalid/2','https://example.invalid/40'],'Unselected photos must never be fetched for email.');
$selection=FDGC_Preclose_Photos::selection([],$available);$event['completionPhotoSelection']=$selection;$request->data['selection']=$selection;$downloads=[];
$result=FDGC_Preclose_Photos::package($request);expect($result['photos']===[]&&$downloads===[],'Explicit no photos cannot fall back to all 60 photos.');
$request->data['selection']['photos']=[['id'=>'3','sheetId'=>'7','name'=>'photo-3.jpg']];
expect(is_wp_error(FDGC_Preclose_Photos::package($request)),'A retry cannot change the committed selection.');
echo "PASS: all job photos retained, selected-only downloads, explicit no-photo completion, immutable event selection, foreign and duplicate photo rejection.\n";
