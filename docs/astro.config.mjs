// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// LATET_DOCS_BUNDLE=1 (`npm run build:bundle`): the copy Latet serves itself, under /_latet/docs/ of the daemon and of
// a Latet server, the documentation of its own version, built into ../src/latet/docs for the wheel and the image. It
// holds no video (a link to watch it on docs.latet.ai instead) and each screenshot at 1440 px only. It has no `site`:
// its canonical and alternate addresses would be pages of docs.latet.ai under /_latet/docs/, which do not exist, and
// without them its pages name no other host but in links.
const bundle = process.env.LATET_DOCS_BUNDLE === '1';
const base = bundle ? '/_latet/docs/' : '/';

// In the bundle the product video's player is never built, and with it neither the videos nor their posters (a module
// that imports them is built whole, shown or not): ProductVideo.astro shows its link instead.
const withoutVideo = {
	name: 'latet:bundle-without-video',
	enforce: 'pre',
	resolveId(source) {
		return source.endsWith('/ProductVideoPlayer.astro') ? '\0latet:no-video-player' : undefined;
	},
	load(id) {
		return id === '\0latet:no-video-player' ? 'export default undefined;' : undefined;
	},
};

// https://astro.build/config
export default defineConfig({
	site: bundle ? undefined : 'https://docs.latet.ai',
	base,
	outDir: bundle ? '../src/latet/docs' : './dist',
	// The components ask whether they build the bundle (src/components/ProductVideo.astro, Screenshot.astro).
	vite: {
		define: { 'import.meta.env.LATET_DOCS_BUNDLE': JSON.stringify(bundle) },
		plugins: bundle ? [withoutVideo] : [],
	},
	// Astro 7 drops the spaces between inline elements by default, which joins words for search and screen
	// readers (a link card's title and its description); keep them, as Astro 5 did.
	compressHTML: true,
	integrations: [
		starlight({
			title: 'Latet',
			// English at the root, the reference; the other languages under their code. A page not translated yet
			// is the English one, with Starlight's notice. The generated reference pages stay English.
			defaultLocale: 'root',
			locales: {
				root: { label: 'English', lang: 'en' },
				it: { label: 'Italiano', lang: 'it' },
				de: { label: 'Deutsch', lang: 'de' },
				fr: { label: 'Français', lang: 'fr' },
				es: { label: 'Español', lang: 'es' },
			},
			// Our own 404 page (src/pages/404.astro): Starlight's looks for a 404 entry in the docs, and Astro warns
			// when there is none.
			disable404Route: true,
			// The footer adds the legal line of latet.ai: who runs the site, the privacy policy, the legal notice.
			components: { Footer: './src/components/Footer.astro' },
			description:
				'A local privacy firewall for AI agents: masks secrets and personal data before they leave your machine and restores them locally.',
			logo: {
				light: './src/assets/latet.png',
				dark: './src/assets/latet-white.png',
				alt: 'Latet',
				replacesTitle: true,
			},
			favicon: '/favicon.svg',
			head: [{ tag: 'link', attrs: { rel: 'apple-touch-icon', href: `${base}apple-touch-icon.png` } }],
			customCss: [
				// The fonts, served by the site itself: Inter for the text, Montserrat for the titles, JetBrains Mono
				// for code.
				'@fontsource-variable/inter/wght.css',
				'@fontsource-variable/inter/wght-italic.css',
				'@fontsource/montserrat/600.css',
				'@fontsource/montserrat/700.css',
				'@fontsource/montserrat/800.css',
				'@fontsource/montserrat/900.css',
				'@fontsource/jetbrains-mono/400.css',
				'@fontsource/jetbrains-mono/700.css',
				'./src/styles/custom.css',
			],
			social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/Latet-AI/latet' }],
			editLink: { baseUrl: 'https://github.com/Latet-AI/latet/edit/main/docs/' },
			sidebar: [
				{
					slug: 'index',
					label: 'Introduction',
					translations: { it: 'Introduzione', de: 'Einführung', fr: 'Introduction', es: 'Introducción' },
				},
				{
					label: 'Getting started',
					translations: { it: 'Per iniziare', de: 'Erste Schritte', fr: 'Premiers pas', es: 'Primeros pasos' },
					items: [
						{ slug: 'getting-started/installation' },
						{ slug: 'getting-started/tutorial' },
						{ slug: 'getting-started/quickstart' },
						{ slug: 'getting-started/docker' },
						{ slug: 'getting-started/server' },
						{ slug: 'getting-started/uninstall' },
					],
				},
				{
					label: 'Concepts',
					translations: { it: 'Concetti', de: 'Konzepte', fr: 'Concepts', es: 'Conceptos' },
					items: [
						{ slug: 'concepts/how-it-works' },
						{ slug: 'concepts/protection-scope' },
						{ slug: 'concepts/server' },
						{ slug: 'concepts/server-operations' },
						{ slug: 'concepts/profiles' },
						{ slug: 'concepts/detection' },
						{ slug: 'concepts/custom-recognizers' },
						{ slug: 'concepts/projects' },
						{ slug: 'concepts/production-data' },
						{ slug: 'concepts/images' },
						{ slug: 'concepts/llm-review' },
						{ slug: 'concepts/live-review' },
						{ slug: 'concepts/agents-and-settings' },
						{ slug: 'concepts/export-import' },
					],
				},
				{
					label: 'Applications',
					translations: { it: 'Applicazioni', de: 'Anwendungen', fr: 'Applications', es: 'Aplicaciones' },
					items: [
						{ slug: 'applications' },
						{ slug: 'applications/logs' },
						{ slug: 'applications/rag' },
						{ slug: 'applications/n8n' },
						{ slug: 'applications/forget' },
					],
				},
				{
					label: 'Agents',
					translations: { it: 'Agenti', de: 'Agenten', fr: 'Agents', es: 'Agentes' },
					items: [
						{ slug: 'agents/claude-code' },
						{ slug: 'agents/codex' },
						{ slug: 'agents/company-endpoints' },
						{ slug: 'agents/opencode' },
						{ slug: 'agents/gemini' },
						{ slug: 'agents/autonomous-agents' },
						{ slug: 'agents/other-tools' },
						{ slug: 'agents/openai-compatible' },
					],
				},
				{ slug: 'dashboard' },
				{
					label: 'Reference',
					translations: { it: 'Riferimento', de: 'Referenz', fr: 'Référence', es: 'Referencia' },
					items: [
						{ slug: 'reference/cli' },
						{ slug: 'reference/configuration' },
						{ slug: 'reference/server-administration' },
						{ slug: 'reference/apps-api' },
						{ slug: 'reference/detectors' },
					],
				},
				{ slug: 'security' },
				{ slug: 'troubleshooting' },
				{ slug: 'contributing' },
				{ slug: 'releasing' },
				{ slug: 'roadmap' },
				{ slug: 'changelog' },
			],
		}),
	],
});
