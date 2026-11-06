<?php
/** Save every job photo before selecting the completion email's attachments. */
if(!defined('ABSPATH'))exit;
final class FDGC_Preclose_Photos {
 public static function refs($s,$r,$j){
  $refs=[];
  foreach(FDGC_Completion_Photos::row_files($s['id'],$r['id']) as $a)if(FDGC_Completion_Photos::selected($j,$a))$refs[$s['id'].':'.$a['id']]=$a+['sheetId'=>(string)$s['id']];
  if(($j['type']??'')==='Project'&&!empty($j['project']['sheetId'])){
   $ps=FDGC_Live_Operations::sheet($j['project']['sheetId'],true);
   foreach($ps['rows']??[] as $pr)foreach(FDGC_Completion_Photos::row_files($ps['id'],$pr['id']) as $a)if(FDGC_Live_Operations::customer_photo($a)&&preg_match('/^Location-photo-/',$a['name']??''))$refs[$ps['id'].':'.$a['id']]=$a+['sheetId'=>(string)$ps['id']];
  }
  return $refs;
 }
 public static function authorized(){
  if(!in_array(FDGC_Live_Operations::access(),['office','tech'],true)||FDGC_Live_Operations::customer_mode())throw new RuntimeException('Operations sign-in required.');
  FDGC_Live_Operations::check_app_nonce();nocache_headers();
  $ctx=FDGC_Live_Operations::context((string)($_POST['key']??''));
  if(!FDGC_Live_Operations::can_dispatch())FDGC_Live_Operations::require_owner($ctx[2]);
  if(($ctx[2]['source']??'')==='history'||in_array($ctx[2]['status'],['Billed','Canceled'],true)||!empty($ctx[2]['billingSubmitted']))throw new RuntimeException('Closed jobs are read-only.');
  return $ctx;
 }
 public static function receipt($key){
  $id=(string)($_POST['submission']??'');if(!preg_match('/^[a-zA-Z0-9-]{10,80}$/',$id))throw new RuntimeException('Reopen the job before reviewing completion.');
  return 'fdgc_preclose_'.hash('sha256',FDGC_Live_Operations::actor_id().'|'.$key.'|'.$id);
 }
 public static function selection($chosen,$available){
  if(!is_array($chosen)||!array_is_list($chosen)||count($chosen)>10)throw new RuntimeException('Choose up to 10 saved photos for the email. All other photos stay on the job.');
  $refs=[];$seen=[];
  foreach($chosen as $key){
   if(!is_string($key)||isset($seen[$key])||!isset($available[$key]))throw new RuntimeException('A selected photo is unavailable. Refresh the photo list and review again.');
   $seen[$key]=true;$a=$available[$key];$refs[]=['id'=>(string)$a['id'],'sheetId'=>(string)$a['sheetId'],'name'=>(string)$a['name']];
  }
  return ['version'=>1,'reviewed'=>true,'photos'=>$refs];
 }
 public static function ajax(){
  $op=(string)($_POST['op']??'');$closing=$op==='status'&&($_POST['status']??'')==='Field complete';
  if(!$closing&&!in_array($op,['preclose_photos','preclose_photo_preview','preclose_selection'],true))return;
  try{
   [$s,$r,$j]=self::authorized();$cache='fdgc_preclose_refs_'.hash('sha256',$j['key']);
   $available=$op==='preclose_photo_preview'?get_transient($cache):false;
   if(!is_array($available))$available=self::refs($s,$r,$j);
   if($op==='preclose_photos'){
    set_transient($cache,$available,30);
    $photos=[];foreach($available as $key=>$a)$photos[]=['key'=>$key,'id'=>(string)$a['id'],'sheetId'=>(string)$a['sheetId'],'name'=>$a['name']];
    wp_send_json_success(['photos'=>$photos]);
   }
   if($op==='preclose_photo_preview'){
    $key=(string)($_POST['photo']??'');if(!isset($available[$key]))throw new RuntimeException('This photo is not available on this job.');
    wp_send_json_success(FDGC_Live_Operations::preview_cached($available[$key],($_POST['thumbnail']??'1')==='1'));
   }
   $receipt=self::receipt($j['key']);
   if($closing){
    $_POST['_fdgc_completion_selection']=null;
    $saved=get_option($receipt,[]);if(empty($saved['selection']))throw new RuntimeException('Upload your job photos, then review which photos go in the completion email.');
    $keys=array_map(static function($a){return $a['sheetId'].':'.$a['id'];},$saved['selection']['photos']);
    $current=self::selection($keys,$available);
    if($current!==$saved['selection'])throw new RuntimeException('The selected photos changed. Review the completion email again.');
    $_POST['_fdgc_completion_selection']=$saved['selection'];return;
   }
   $chosen=json_decode(wp_unslash((string)($_POST['photos']??'')),true);
   if($chosen===[]&&($_POST['withoutPhotos']??'')!=='1')throw new RuntimeException('Select email photos or explicitly choose Send without photos.');
   $selection=self::selection($chosen,$available);
   $lock=FDGC_Live_Operations::lock();try{update_option($receipt,['selection'=>$selection,'at'=>gmdate('c'),'by'=>FDGC_Live_Operations::actor()->display_name],false);}finally{FDGC_Live_Operations::unlock($lock);}
   wp_send_json_success(['selection'=>$selection]);
  }catch(Throwable $e){wp_send_json_error(['message'=>$e->getMessage()],400);}
 }
 public static function package($request){
  try{
   $p=$request->get_json_params();$key=(string)($p['jobKey']??'');$selection=null;
   if(isset($p['selection'])){
    global $wpdb;$id=(string)($p['eventId']??'');
    $json=$wpdb->get_var($wpdb->prepare('SELECT payload FROM '.$wpdb->prefix.'fdgc_activity WHERE event_id=%s',$id));$event=json_decode((string)$json,true);
    if(!$event||empty($event['committed'])||($event['jobKey']??'')!==$key||($event['status']??'')!=='Field complete'||($event['completionPhotoSelection']??null)!==$p['selection'])throw new RuntimeException('The completion photo selection could not be verified against its saved update.');
    $selection=$event['completionPhotoSelection'];
   }
   if(!preg_match('/^(florence|columbia):([0-9]+)$/',$key,$parts))throw new RuntimeException('Invalid operational job.');
   $map=FDGC_Live_Operations::mapping();$s=FDGC_Live_Operations::sheet($map[$parts[1]]['id'],true);$r=null;
   foreach($s['rows']??[] as $row)if((string)$row['id']===$parts[2]){$r=$row;break;}
   if(!$r)throw new RuntimeException('Completion job moved or is unavailable.');
   $j=FDGC_Live_Operations::job($r,$s,$parts[1],false);$available=self::refs($s,$r,$j);$refs=$available;
   if($selection!==null){
    $keys=array_map(static function($a){return $a['sheetId'].':'.$a['id'];},$selection['photos']);
    if(self::selection($keys,$available)!==$selection)throw new RuntimeException('A reviewed email photo changed or is no longer available.');
    $refs=array_intersect_key($available,array_fill_keys($keys,true));
   }elseif(count($refs)>20)throw new RuntimeException('The legacy completion photo package needs office review.');
   $photos=[];$seen=[];$budget=(int)floor((14*1024*1024)/max(1,count($refs)));
   foreach($refs as $a){
    if(($a['sizeInKb']??0)>15360)throw new RuntimeException('A selected photo exceeds the preparation limit.');
    $meta=FDGC_Live_Operations::api('GET','/sheets/'.$a['sheetId'].'/attachments/'.$a['id']);$url=$meta['url']??'';
    if(strpos($url,'https://')!==0)throw new RuntimeException('Selected photo download is unavailable.');
    $reply=wp_remote_get($url,['timeout'=>20,'limit_response_size'=>15728641]);
    if(is_wp_error($reply)||wp_remote_retrieve_response_code($reply)!==200)throw new RuntimeException('A selected photo could not be downloaded.');
    $bytes=wp_remote_retrieve_body($reply);if(strlen($bytes)>15728640)throw new RuntimeException('A selected photo is too large.');
    $hash=hash('sha256',$bytes);if(isset($seen[$hash]))continue;$seen[$hash]=true;
    $raw=FDGC_Completion_Photos::optimize($bytes,$budget);$photos[]=['name'=>'Field-photo-'.(count($photos)+1).'.jpg','content'=>base64_encode($raw),'sha256'=>hash('sha256',$raw)];
   }
   $result=['jobKey'=>$key,'photos'=>$photos];if($selection!==null)$result['selection']=$selection;return $result;
  }catch(Throwable $e){return new WP_Error('completion_photo_selection',$e->getMessage(),['status'=>409]);}
 }
 public static function page(){
  $path=trim((string)parse_url($_SERVER['REQUEST_URI']??'',PHP_URL_PATH),'/');if(!in_array($path,['graycliff-office','graycliff-tech'],true))return;
  ob_start(static function($html){
   $js=<<<'FDGC_PRECLOSE_JS'
/*PRECLOSE_SCRIPT*/
FDGC_PRECLOSE_JS;
   return preg_replace_callback('/let completionPhotoBatch=null;.*?\},true\);/s',static function()use($js){return $js;},$html,1);
  });
 }
}
add_action('wp_ajax_fdgc_live',['FDGC_Preclose_Photos','ajax'],-150);
add_action('wp_ajax_nopriv_fdgc_live',['FDGC_Preclose_Photos','ajax'],-150);
add_action('rest_api_init',static function(){register_rest_route('fdgc/v1','/completion-photos',['methods'=>'POST','permission_callback'=>['FDGC_Live_Operations','bridge_authorized'],'callback'=>['FDGC_Preclose_Photos','package']],true);},2000);
add_action('template_redirect',['FDGC_Preclose_Photos','page'],-100000);
