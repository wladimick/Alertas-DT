<?php
defined( 'ABSPATH' ) || exit;

class ADT_Admin {

    public static function register(): void {
        add_action( 'admin_menu',    [ __CLASS__, 'add_menu' ] );
        add_action( 'admin_post_adt_regenerate_token', [ __CLASS__, 'handle_regenerate' ] );
        add_action( 'admin_enqueue_scripts', [ __CLASS__, 'enqueue_assets' ] );
    }

    public static function enqueue_assets( string $hook ): void {
        if ( strpos( $hook, 'alertas-dt' ) === false ) {
            return;
        }
        wp_enqueue_style(
            'alertas-dt-admin',
            ADT_PLUGIN_URL . 'assets/css/admin.css',
            [],
            ADT_VERSION
        );
    }

    public static function add_menu(): void {
        add_menu_page(
            'Alertas DT + SII',
            'Alertas DT + SII',
            'manage_options',
            'alertas-dt',
            [ __CLASS__, 'render_page' ],
            'dashicons-email-alt',
            80
        );
    }

    /** Clave del transient de revelación de token (por usuario, TTL 60 s). */
    private static function reveal_key(): string {
        return 'adt_token_reveal_' . get_current_user_id();
    }

    public static function handle_regenerate(): void {
        if ( ! current_user_can( 'manage_options' ) ) {
            wp_die( 'Sin permisos.' );
        }
        check_admin_referer( 'adt_regenerate_token' );
        $new_token = ADT_Settings::regenerate_token();
        // Guardar token completo en transient de un solo uso (60 s, solo para este usuario).
        set_transient( self::reveal_key(), $new_token, 60 );
        wp_safe_redirect( add_query_arg( 'adt_notice', 'token_regenerated', admin_url( 'admin.php?page=alertas-dt' ) ) );
        exit;
    }

    public static function render_page(): void {
        if ( ! current_user_can( 'manage_options' ) ) {
            wp_die( 'Sin permisos.' );
        }

        $notice    = sanitize_text_field( $_GET['adt_notice'] ?? '' );
        $token     = ADT_Settings::get_token();
        $masked    = $token ? substr( $token, 0, 8 ) . str_repeat( '•', 20 ) : '(sin token)';
        $total     = ADT_Database::count();
        $active    = ADT_Database::count( 'active' );
        $last_sync = ADT_Settings::get_last_sync() ?: '—';
        $base_url  = rest_url( ADT_REST::NAMESPACE );

        // Consumir el transient de revelación (solo se muestra una vez).
        $reveal_token = get_transient( self::reveal_key() );
        if ( $reveal_token ) {
            delete_transient( self::reveal_key() );
        }
        ?>
        <div class="wrap adt-admin">
            <h1>Alertas DT + SII <span class="adt-version">v<?php echo esc_html( ADT_VERSION ); ?></span></h1>

            <?php if ( $reveal_token ) : ?>
                <div class="notice notice-warning adt-token-reveal" style="border-left-color:#d97706;">
                    <h3 style="margin:.5em 0 .25em;">⚠️ Token nuevo generado — cópialo ahora</h3>
                    <p>Este token completo se muestra <strong>una sola vez</strong>. Al recargar la página quedará enmascarado.</p>
                    <div class="adt-token-reveal__box">
                        <code id="adt-token-full" class="adt-token-full"><?php echo esc_html( $reveal_token ); ?></code>
                        <button type="button" class="button button-primary" id="adt-copy-btn"
                                onclick="adtCopyToken()">Copiar token</button>
                    </div>
                    <p class="adt-reveal-note">
                        Pega este valor en <code>WORDPRESS_API_TOKEN</code> en el entorno de la app local.
                        <strong>No lo guardes en el repositorio.</strong>
                    </p>
                </div>
                <script>
                function adtCopyToken() {
                    var val = document.getElementById('adt-token-full').textContent;
                    if (navigator.clipboard) {
                        navigator.clipboard.writeText(val).then(function() {
                            var btn = document.getElementById('adt-copy-btn');
                            btn.textContent = '¡Copiado!';
                            btn.disabled = true;
                            setTimeout(function() {
                                btn.textContent = 'Copiar token';
                                btn.disabled = false;
                            }, 3000);
                        });
                    } else {
                        // Fallback para navegadores sin Clipboard API
                        var ta = document.createElement('textarea');
                        ta.value = val;
                        ta.style.position = 'fixed';
                        ta.style.opacity = '0';
                        document.body.appendChild(ta);
                        ta.select();
                        document.execCommand('copy');
                        document.body.removeChild(ta);
                        var btn = document.getElementById('adt-copy-btn');
                        btn.textContent = '¡Copiado!';
                        setTimeout(function() { btn.textContent = 'Copiar token'; }, 3000);
                    }
                }
                </script>
            <?php elseif ( 'token_regenerated' === $notice ) : ?>
                <div class="notice notice-error is-dismissible">
                    <p><strong>El token ya no está disponible para mostrar.</strong> Si no lo copiaste, genera uno nuevo con el botón "Regenerar token".</p>
                </div>
            <?php endif; ?>

            <div class="adt-cards">

                <div class="adt-card">
                    <h2>Estado</h2>
                    <p><span class="adt-badge adt-badge--ok">Activo</span></p>
                    <p>Versión <strong><?php echo esc_html( ADT_VERSION ); ?></strong></p>
                </div>

                <div class="adt-card">
                    <h2>Suscriptores</h2>
                    <p class="adt-stat"><?php echo esc_html( $total ); ?></p>
                    <p class="adt-muted"><?php echo esc_html( $active ); ?> activos</p>
                    <p class="adt-muted">Última sincronización: <?php echo esc_html( $last_sync ); ?></p>
                </div>

                <div class="adt-card">
                    <h2>Shortcode</h2>
                    <code class="adt-code">[alertas_dt_form]</code>
                    <p class="adt-muted">Pégalo en cualquier página o entrada de WordPress.</p>
                </div>

            </div>

            <div class="adt-section">
                <h2>API para app local</h2>
                <table class="form-table">
                    <tr>
                        <th>Endpoint base</th>
                        <td><code><?php echo esc_html( $base_url ); ?></code></td>
                    </tr>
                    <tr>
                        <th>Suscriptores</th>
                        <td><code><?php echo esc_html( $base_url . '/subscribers' ); ?></code></td>
                    </tr>
                    <tr>
                        <th>Healthcheck</th>
                        <td><code><?php echo esc_html( $base_url . '/health' ); ?></code></td>
                    </tr>
                    <tr>
                        <th>Token API</th>
                        <td>
                            <code class="adt-token-masked"><?php echo esc_html( $masked ); ?></code>
                            <form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>"
                                  style="display:inline-block; margin-left: 12px;">
                                <?php wp_nonce_field( 'adt_regenerate_token' ); ?>
                                <input type="hidden" name="action" value="adt_regenerate_token">
                                <button type="submit" class="button button-secondary"
                                        onclick="return confirm('¿Regenerar el token? La app local dejará de sincronizar hasta que actualices WORDPRESS_API_TOKEN.')">
                                    Regenerar token
                                </button>
                            </form>
                            <p class="adt-muted" style="margin-top:6px;">
                                Al regenerar, el token completo aparecerá <strong>una sola vez</strong> para que puedas copiarlo.
                            </p>
                        </td>
                    </tr>
                </table>
            </div>

            <div class="adt-section">
                <h2>Configurar app Python local</h2>
                <p>Agrega estas variables de entorno en el computador donde corre la app.<br>
                   <strong>Copia el token desde el banner que aparece al regenerarlo y pégalo en <code>WORDPRESS_API_TOKEN</code>.</strong></p>
                <pre class="adt-pre">WORDPRESS_SYNC_ENABLED=true
WORDPRESS_API_URL=<?php echo esc_html( rtrim( $base_url, '/' ) ); ?>

WORDPRESS_API_TOKEN=<em>pega aquí el token — aparece al regenerar</em>
WORDPRESS_SYNC_INTERVAL_MINUTES=15
WORDPRESS_SYNC_LIMIT=100</pre>
                <p class="adt-muted">
                    <strong>No compartas el token ni lo guardes en el repositorio.</strong>
                    Si lo pierdes, genera uno nuevo con "Regenerar token" y actualiza la variable en la app local.
                </p>
            </div>

        </div>
        <?php
    }
}
