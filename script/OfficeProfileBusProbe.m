#import <Foundation/Foundation.h>
// Notification transport test only. No cursor setters or Office APIs.
@interface ProfileBusProbe : NSObject
@property NSUInteger count;
@end
@implementation ProfileBusProbe
- (void)profile:(NSNotification *)note {
    if(![note.object isKindOfClass:NSString.class])return;
    puts([note.object UTF8String]);fflush(stdout);self.count++;
    if(self.count==1)[NSDistributedNotificationCenter.defaultCenter
        postNotificationName:@"local.codex.UUDisplayAutomation.OfficeCursorProfileRequest"
        object:nil userInfo:nil options:NSNotificationDeliverImmediately];
    if(self.count==3)CFRunLoopStop(CFRunLoopGetMain());
}
@end
int main(void){@autoreleasepool{
    ProfileBusProbe *probe=[ProfileBusProbe new];
    [NSDistributedNotificationCenter.defaultCenter addObserver:probe selector:@selector(profile:)
        name:@"local.codex.UUDisplayAutomation.OfficeCursorProfile" object:nil
        suspensionBehavior:NSNotificationSuspensionBehaviorDeliverImmediately];
    puts("READY");fflush(stdout);
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW,10*NSEC_PER_SEC),dispatch_get_main_queue(),^{CFRunLoopStop(CFRunLoopGetMain());});
    CFRunLoopRun();[NSDistributedNotificationCenter.defaultCenter removeObserver:probe];
    return probe.count==3?0:2;
}}
