from datetime import timedelta
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Category, Comment, Post


class AccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.author = users.create_user(
            username='author', email='author@example.com',
            password='test-password-4281',
        )
        cls.other = users.create_user(username='other')
        cls.category = Category.objects.create(
            title='Category', slug='category', description='Description',
        )
        cls.post = Post.objects.create(
            title='Public post', text='Text', author=cls.author,
            category=cls.category, pub_date=timezone.now(),
        )
        cls.comment = Comment.objects.create(
            post=cls.post, author=cls.author, text='Original comment',
        )

    def test_hidden_posts_are_available_only_to_author(self):
        self.post.pub_date = timezone.now() + timedelta(days=1)
        self.post.is_published = False
        self.post.save()
        url = reverse('blog:post_detail', args=[self.post.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.get(
            reverse('blog:profile', args=[self.author.username]),
        )
        self.assertContains(response, self.post.title)

    def test_author_can_save_publish_and_unpublish_draft(self):
        self.client.force_login(self.author)
        profile_url = reverse('blog:profile', args=[self.author.username])
        response = self.client.get(reverse('blog:create_post'))
        self.assertContains(response, 'name="is_published"')
        self.assertTrue(response.context['form']['is_published'].value())
        data = {
            'title': 'Publication lifecycle',
            'text': 'Draft text',
            'pub_date': (timezone.now() - timedelta(days=1)).strftime(
                '%Y-%m-%dT%H:%M',
            ),
            'category': self.category.pk,
        }
        response = self.client.post(reverse('blog:create_post'), data)
        self.assertRedirects(response, profile_url)
        post = Post.objects.get(title=data['title'])
        self.assertEqual(post.author, self.author)
        self.assertFalse(post.is_published)
        detail_url = reverse('blog:post_detail', args=[post.pk])
        edit_url = reverse('blog:edit_post', args=[post.pk])
        public_urls = (
            reverse('blog:index'), profile_url,
            reverse('blog:category_posts', args=[self.category.slug]),
        )
        visitor = Client()
        for published in (False, True, False):
            with self.subTest(is_published=published):
                if published:
                    data['is_published'] = 'on'
                else:
                    data.pop('is_published', None)
                self.assertRedirects(
                    self.client.post(edit_url, data), detail_url,
                )
                post.refresh_from_db()
                self.assertEqual(post.is_published, published)
                self.assertEqual(Post.objects.count(), 2)
                response = self.client.get(edit_url)
                self.assertEqual(
                    response.context['form']['is_published'].value(),
                    published,
                )
                self.assertContains(self.client.get(profile_url), post.title)
                self.assertContains(self.client.get(detail_url), post.title)
                for url in public_urls:
                    response = visitor.get(url)
                    if published:
                        self.assertContains(response, post.title)
                    else:
                        self.assertNotContains(response, post.title)
                self.assertEqual(
                    visitor.get(detail_url).status_code,
                    200 if published else 404,
                )

    def test_other_user_cannot_publish_draft(self):
        self.post.is_published = False
        self.post.save()
        self.client.force_login(self.other)
        response = self.client.post(reverse('blog:edit_post', args=[
            self.post.pk,
        ]), {
            'title': self.post.title,
            'text': self.post.text,
            'pub_date': self.post.pub_date.isoformat(),
            'category': self.category.pk,
            'is_published': 'on',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('blog:post_detail', args=[
            self.post.pk,
        ]))
        self.post.refresh_from_db()
        self.assertFalse(self.post.is_published)
        self.assertEqual(self.post.author, self.author)

    def test_comment_url_must_match_its_post(self):
        self.client.force_login(self.author)
        for name in ('edit_comment', 'delete_comment'):
            url = reverse('blog:' + name, args=[
                self.post.pk + 100, self.comment.pk,
            ])
            response = self.client.post(url, {'text': 'Changed'})
            self.assertEqual(response.status_code, 404)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.text, 'Original comment')

    def test_get_does_not_delete_content(self):
        self.client.force_login(self.author)
        for name, args in (
            ('delete_post', [self.post.pk]),
            ('delete_comment', [self.post.pk, self.comment.pk]),
        ):
            self.assertEqual(self.client.get(
                reverse('blog:' + name, args=args),
            ).status_code, 200)
        self.assertTrue(Post.objects.filter(pk=self.post.pk).exists())
        self.assertTrue(Comment.objects.filter(pk=self.comment.pk).exists())

    def test_other_user_cannot_modify_content(self):
        self.client.force_login(self.other)
        for name in ('edit_comment', 'delete_comment'):
            self.client.post(reverse('blog:' + name, args=[
                self.post.pk, self.comment.pk,
            ]), {'text': 'Changed'})
        self.client.post(reverse('blog:delete_post', args=[self.post.pk]))
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.text, 'Original comment')
        self.assertTrue(Post.objects.filter(pk=self.post.pk).exists())

    def test_csrf_is_required_for_comments(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.author)
        response = client.post(
            reverse('blog:add_comment', args=[self.post.pk]),
            {'text': 'Without token'},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Comment.objects.count(), 1)

    def test_password_reset_writes_email_to_file(self):
        with TemporaryDirectory() as directory:
            with override_settings(
                EMAIL_BACKEND=(
                    'django.core.mail.backends.filebased.EmailBackend'
                ),
                EMAIL_FILE_PATH=directory,
            ):
                response = self.client.post(reverse('password_reset'), {
                    'email': self.author.email,
                })
                self.assertRedirects(response, reverse('password_reset_done'))
                self.assertEqual(len(list(Path(directory).glob('*.log'))), 1)

    def test_signup_creates_user_without_staff_privileges(self):
        response = self.client.post(reverse('registration'), {
            'username': 'new-user',
            'password1': 'another-strong-password-7385',
            'password2': 'another-strong-password-7385',
            'is_staff': True,
        })
        self.assertRedirects(response, reverse('login'))
        user = get_user_model().objects.get(username='new-user')
        self.assertFalse(user.is_staff)
        self.assertTrue(user.check_password('another-strong-password-7385'))

    def test_logout_renders_page_and_ends_session(self):
        self.client.force_login(self.author)
        response = self.client.post(reverse('logout'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'registration/logged_out.html')
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(self.client.get(reverse('logout')).status_code, 405)

    def test_deleted_category_does_not_break_author_pages(self):
        self.category.delete()
        self.post.refresh_from_db()
        self.assertIsNone(self.post.category)
        self.client.force_login(self.author)
        for url in (
            reverse('blog:profile', args=[self.author.username]),
            reverse('blog:post_detail', args=[self.post.pk]),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertContains(response, self.post.title)
                self.assertContains(response, 'Без категории')
        for name in ('edit_post', 'delete_post'):
            self.assertEqual(self.client.get(
                reverse('blog:' + name, args=[self.post.pk]),
            ).status_code, 200)
        self.client.logout()
        response = self.client.get(reverse('blog:profile', args=[
            self.author.username,
        ]))
        self.assertNotContains(response, self.post.title)
        self.assertEqual(self.client.get(reverse(
            'blog:post_detail', args=[self.post.pk],
        )).status_code, 404)

    def test_scheduled_post_becomes_public_at_publication_time(self):
        publication_time = timezone.now() + timedelta(days=1)
        self.post.pub_date = publication_time
        self.post.save()
        list_urls = (
            reverse('blog:index'),
            reverse('blog:profile', args=[self.author.username]),
            reverse('blog:category_posts', args=[self.category.slug]),
        )
        detail_url = reverse('blog:post_detail', args=[self.post.pk])
        for now, visible in (
            (publication_time - timedelta(seconds=1), False),
            (publication_time, True),
        ):
            with self.subTest(visible=visible):
                with patch('blog.views.timezone.now', return_value=now):
                    for url in list_urls:
                        response = self.client.get(url)
                        if visible:
                            self.assertContains(response, self.post.title)
                        else:
                            self.assertNotContains(response, self.post.title)
                    self.assertEqual(
                        self.client.get(detail_url).status_code,
                        200 if visible else 404,
                    )

    def test_password_reset_link_changes_password(self):
        with TemporaryDirectory() as directory:
            with override_settings(
                EMAIL_BACKEND=(
                    'django.core.mail.backends.filebased.EmailBackend'
                ),
                EMAIL_FILE_PATH=directory,
            ):
                self.client.post(reverse('password_reset'), {
                    'email': self.author.email,
                })
                email_path = next(Path(directory).glob('*.log'))
                email = email_path.read_text(encoding='utf-8')
        match = re.search(r'http://testserver(/[^\s]+)', email)
        self.assertIsNotNone(match)
        response = self.client.get(match.group(1), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response, 'registration/password_reset_confirm.html',
        )
        password = 'new-secure-password-9384'
        response = self.client.post(response.request['PATH_INFO'], {
            'new_password1': password,
            'new_password2': password,
        }, follow=True)
        self.assertTemplateUsed(
            response, 'registration/password_reset_complete.html',
        )
        self.author.refresh_from_db()
        self.assertTrue(self.author.check_password(password))
        response = self.client.get(match.group(1), follow=True)
        self.assertContains(response, 'Запросить новую ссылку')
        self.assertNotContains(response, 'name="new_password1"')

    def test_password_change_keeps_user_logged_in(self):
        self.client.force_login(self.author)
        password = 'changed-secure-password-1739'
        response = self.client.post(reverse('password_change'), {
            'old_password': 'test-password-4281',
            'new_password1': password,
            'new_password2': password,
        }, follow=True)
        self.assertTemplateUsed(
            response, 'registration/password_change_done.html',
        )
        self.author.refresh_from_db()
        self.assertTrue(self.author.check_password(password))
        self.assertEqual(
            str(self.author.pk), self.client.session['_auth_user_id'],
        )

    def test_invalid_password_reset_link_is_handled(self):
        response = self.client.get(reverse('password_reset_confirm', args=[
            'invalid-user', 'invalid-token',
        ]))
        self.assertContains(response, 'Запросить новую ссылку')
        self.assertNotContains(response, 'name="new_password1"')

    @override_settings(DEBUG=False)
    def test_server_errors_use_custom_template(self):
        client = Client(raise_request_exception=False)
        with patch('blog.views.published_posts', side_effect=RuntimeError):
            response = client.get(reverse('blog:index'))
        self.assertEqual(response.status_code, 500)
        self.assertTemplateUsed(response, 'pages/500.html')
