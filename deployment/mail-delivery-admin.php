<?php
// Office-only delivery receipts remain available after dashboard alerts are cleared.
add_action('admin_notices',static function(){
 if(($_GET['page']??'')!=='fdgc-operations'||!class_exists('FDGC_Live_Operations')||FDGC_Live_Operations::access()!=='office'||FDGC_Live_Operations::customer_mode())return;
 $report=get_option('fdgc_mail_report',[]);
 echo '<div class="notice notice-info" style="padding:16px"><h2>Completion / field update email delivery</h2><p>Clearing a dashboard error acknowledges the alert. Delivery receipts remain here.</p><p>Sending: <strong>'.esc_html($report['sending']??'Unknown').'</strong> · Last checked: '.esc_html($report['checkedAt']??'Not reported').'</p><table class="widefat striped"><thead><tr><th>Job</th><th>Email status</th><th>Result</th><th>Photo package</th></tr></thead><tbody>';
 foreach($report['drafts']??[] as $draft){
  $note='';if(preg_match('/[0-9]+ field photo\(s\) attached(?: in Field-photos\.zip)?\./',(string)($draft['text']??''),$match))$note=$match[0];
  echo '<tr><td>'.esc_html($draft['title']??$draft['jobKey']??'').'</td><td>'.esc_html($draft['state']??'Unknown').'</td><td>'.esc_html($draft['detail']??'').'</td><td>'.esc_html($note).'</td></tr>';
 }
 echo '</tbody></table><p>ACCEPTED means Microsoft accepted the email; it does not confirm recipient delivery.</p></div>';
});
