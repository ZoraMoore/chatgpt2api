"""在生成网络操作开始前取得一次许可；不用于原会话读取或图片下载。"""
def guard_image_submission(backend):
    if getattr(backend, '_image_generation_committed', False) is True:
        return
    guard = getattr(backend, '_image_submission_guard', None)
    if callable(guard):
        guard()
    # 取得许可后即视为提交可能发生，后续网络异常不能据此换号重放。
    backend._image_generation_committed = True
