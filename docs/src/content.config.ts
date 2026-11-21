import { defineCollection } from 'astro:content';
import { z } from 'astro/zod';
import { docsLoader, i18nLoader } from '@astrojs/starlight/loaders';
import { docsSchema, i18nSchema } from '@astrojs/starlight/schema';

export const collections = {
	docs: defineCollection({ loader: docsLoader(), schema: docsSchema() }),
	// UI strings we add ourselves, one file per locale in src/content/i18n/.
	// Starlight's own UI strings are already translated and can be overridden here too. Pagefind's search strings
	// (pagefind.*) are set here for it, de and fr: Starlight ships them only for es, and Pagefind's module build never
	// reads the page's language, so without them the search answers in English (tests/unit/test_docs_search_texts.py).
	i18n: defineCollection({
		loader: i18nLoader(),
		schema: i18nSchema({
			extend: z.object({
				// The footer's legal line (src/components/Footer.astro).
				'latet.footer.vat': z.string().optional(),
				'latet.footer.privacy': z.string().optional(),
				'latet.footer.legal': z.string().optional(),
				// The product video's description for screen readers (src/components/ProductVideoPlayer.astro).
				'latet.video.label': z.string().optional(),
				// The link that stands for the video in the copy Latet serves itself (src/components/ProductVideo.astro).
				'latet.video.watch': z.string().optional(),
			}),
		}),
	}),
};
