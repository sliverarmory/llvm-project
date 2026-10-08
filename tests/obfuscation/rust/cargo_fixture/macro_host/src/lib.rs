use proc_macro::TokenStream;

#[proc_macro]
pub fn identity(input: TokenStream) -> TokenStream {
    input
}
