"""Seed the anime project catalog used by ticket aggregators."""

from alembic import op

revision = "0002_anime_catalog_ips"
down_revision = "0001_genchi_curated"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO "Ip" ("id","slug","nameJa","nameZh","colorHex","sortOrder") VALUES
            ('ip-anime-general','anime-general','アニメ・ゲーム','二次元综合','#7357D9',5),
            ('ip-project-sekai','project-sekai','プロジェクトセカイ','世界计划','#39B8D4',50),
            ('ip-d4dj','d4dj','D4DJ','D4DJ','#E83E8C',60),
            ('ip-revue-starlight','revue-starlight','少女☆歌劇 レヴュースタァライト','少女歌剧','#F0A41E',70),
            ('ip-from-argonavis','from-argonavis','from ARGONAVIS','ARGONAVIS','#296BD6',80),
            ('ip-uma-musume','uma-musume','ウマ娘 プリティーダービー','赛马娘','#70B44C',90),
            ('ip-idoly-pride','idoly-pride','IDOLY PRIDE','IDOLY PRIDE','#775BC7',100),
            ('ip-tokyo-7th-sisters','tokyo-7th-sisters','Tokyo 7th シスターズ','东京 7th Sisters','#EF6C87',110),
            ('ip-22-7','22-7','22/7','22/7','#62A9E8',120),
            ('ip-denonbu','denonbu','電音部','电音部','#8B5CF6',130),
            ('ip-world-dai-star','world-dai-star','ワールドダイスター','世界大明星','#E55C9B',140),
            ('ip-aikatsu','aikatsu','アイカツ！','偶像活动','#F08AB4',150),
            ('ip-pretty-series','pretty-series','プリティーシリーズ','美妙系列','#A855C7',160),
            ('ip-macross','macross','マクロス','超时空要塞','#3E88C8',170),
            ('ip-symphogear','symphogear','戦姫絶唱シンフォギア','战姬绝唱','#D84C62',180),
            ('ip-bocchi-the-rock','bocchi-the-rock','ぼっち・ざ・ろっく！','孤独摇滚！','#E85D75',190),
            ('ip-zombie-land-saga','zombie-land-saga','ゾンビランドサガ','佐贺偶像是传奇','#5B8C85',200),
            ('ip-ensemble-stars','ensemble-stars','あんさんぶるスターズ！','偶像梦幻祭','#4E9FCF',210),
            ('ip-idolish7','idolish7','アイドリッシュセブン','IDOLiSH7','#F28C28',220),
            ('ip-hypnosis-mic','hypnosis-mic','ヒプノシスマイク','催眠麦克风','#D94343',230),
            ('ip-uta-no-prince-sama','uta-no-prince-sama','うたの☆プリンスさまっ♪','歌之王子殿下','#446CCF',240)
        ON CONFLICT ("slug") DO UPDATE SET
            "nameJa"=COALESCE("Ip"."nameJa",EXCLUDED."nameJa"),
            "nameZh"=COALESCE("Ip"."nameZh",EXCLUDED."nameZh"),
            "colorHex"=COALESCE("Ip"."colorHex",EXCLUDED."colorHex");

        UPDATE "Ip" SET "colorHex"=COALESCE("colorHex",'#E84076')
        WHERE "slug"='bang-dream';
        UPDATE "Ip" SET "colorHex"=COALESCE("colorHex",'#D94B4B')
        WHERE "slug"='girls-band-cry';
        UPDATE "Ip" SET "colorHex"=COALESCE("colorHex",'#24A9E8')
        WHERE "slug"='love-live';
        UPDATE "Ip" SET "colorHex"=COALESCE("colorHex",'#F28C28')
        WHERE "slug"='idolmaster';
        """
    )


def downgrade() -> None:
    # Reference data may already be attached to curated Events, so downgrades
    # intentionally preserve it.
    pass
