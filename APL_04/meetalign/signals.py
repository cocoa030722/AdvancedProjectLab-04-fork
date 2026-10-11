from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import Recording


@receiver(post_delete, sender=Recording)
def delete_recording_file(sender, instance, **kwargs):
    """DB 행 삭제(연쇄 삭제 포함) 시 MEDIA_ROOT의 녹음 파일도 함께 지운다."""
    if instance.file:
        instance.file.delete(save=False)
